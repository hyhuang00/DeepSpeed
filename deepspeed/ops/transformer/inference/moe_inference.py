'''
Copyright 2020 The Microsoft DeepSpeed Team
'''
import json
import math
import importlib
import torch
from torch import nn
from torch.autograd import Function
import time
from ... import op_builder
#from ...inference.engine import inference_cuda_module, specialized_mode
# Cuda modules will be imported if needed
inference_cuda_module = None
specialized_mode = None
import torch.nn as nn
from .transformer_inference import DeepSpeedSelfAttention, DeepSpeedInferenceConfig
from ....moe.sharded_moe import TopKGate
import torch.distributed as dist

import torch.nn.functional as F


class DeepSpeedMoEInferenceConfig(DeepSpeedInferenceConfig):
    """Initialize the DeepSpeed Transformer Config.
        Arguments:
            hidden_size: The hidden size of the transformer layer
            intermediate_size: The intermediate size of the feed-forward part of transformer layer
            heads: The number of heads in the self-attention of the transformer layer
            num_hidden_layers: The number of transformer layers
            layer_norm_eps: The epsilon value for the layer norm
            local_rank: Optional: The rank of GPU running the transformer kernel, it is not required
                to use if the model already set the current device, otherwise need to set it
                so that the transformer kernel can work on the right device
            mp_size (optional): This argument is mainly used to create the parameters on the kernel side
                using model-parallel architecture. If the client model already takes care of this, there is no
                need to pass this argument.
            fp16: Enable half-precision computation
            pre_layer_norm: Select between Pre-LN or Post-LN transformer architecture
            stochastic_mode:  Enable for high performance, please note that this flag has some level of
                non-determinism and can produce different results on different runs.  However, we have seen
                that by enabling it, the pretraining tasks such as BERT are not affected and can obtain
                a high accuracy level. On the other hand, for the downstream tasks, such as fine-tuning, we recommend
                to turn it off in order to be able to reproduce the same result through the regular kernel execution.

            scale_attention: If true, both q and k are scaled by 1/sqrt(attention_heads) before attention computation.
            return_tuple: if True, returns the transformer output as a tuple, otherwise returns as a tensor
    """
    def __init__(self,
                 hidden_size=-1,
                 intermediate_size=-1,
                 heads=-1,
                 num_hidden_layers=-1,
                 layer_norm_eps=1e-12,
                 local_rank=-1,
                 mp_size=1,
                 fp16=False,
                 q_int8=False,
                 pre_layer_norm=True,
                 stochastic_mode=False,
                 scale_attention=True,
                 triangular_masking=True,
                 local_attention=False,
                 window_size=256,
                 return_tuple=True,
                 moe_experts=1,
                 global_experts=1,
                 k=1,
                 capacity_factor=1.,
                 eval_capacity_factor=1.,
                 min_capacity=1,
                 noisy_gate_policy=None,
                 drop_tokens=True,
                 use_rts=False,
                 mlp_type='standard'):
        super(DeepSpeedMoEInferenceConfig,
              self).__init__(
                  hidden_size,
                  (intermediate_size if intermediate_size > 0 else 4 * hidden_size),
                  heads,
                  num_hidden_layers,
                  layer_norm_eps,
                  local_rank,
                  mp_size,
                  fp16,
                  q_int8,
                  pre_layer_norm,
                  stochastic_mode,
                  scale_attention,
                  triangular_masking,
                  local_attention,
                  window_size,
                  return_tuple)
        self.moe_experts = moe_experts
        self.k = k
        self.capacity_factor = capacity_factor
        self.eval_capacity_factor = eval_capacity_factor
        self.min_capacity = min_capacity
        self.noisy_gate_policy = noisy_gate_policy
        self.drop_tokens = drop_tokens
        self.use_rts = use_rts
        self.global_experts = global_experts
        self.mlp_type = mlp_type

    @classmethod
    def from_dict(cls, json_object):
        config = DeepSpeedInferenceConfig()
        for key, value in json_object.items():
            config.__dict__[key] = value
        return config

    @classmethod
    def from_json_file(cls, json_file):
        with open(json_file, "r", encoding='utf-8') as reader:
            text = reader.read()
        return cls.from_dict(json.loads(text))


class DeepSpeedMLPFunction(Function):
    @staticmethod
    def forward(ctx,
                input,
                inter_w,
                inter_b,
                config,
                output_b,
                output_w,
                q_scales,
                q_groups,
                merge_count,
                mp_group,
                async_op):
        if config.q_int8:
            intermediate = inference_cuda_module.fused_gemm_gelu_int8(
                input,
                inter_w,
                inter_b,
                config.epsilon,
                q_scales[2],
                (q_groups * (2**merge_count)),
                config.pre_layer_norm)
            output = inference_cuda_module.vector_matmul_int8(intermediate,
                                                              output_w,
                                                              q_scales[3],
                                                              q_groups,
                                                              (merge_count))
        else:
            mlp_gemm_func = inference_cuda_module.fused_gemm_gelu_fp16 if config.fp16 else \
                                    inference_cuda_module.fused_gemm_gelu_fp32

            output = mlp_gemm_func(input,
                                   inter_w,
                                   inter_b,
                                   output_w,
                                   config.epsilon,
                                   config.pre_layer_norm,
                                   async_op)
        if mp_group is not None and dist.get_world_size(group=mp_group) > 1:
            dist.all_reduce(output, group=mp_group, async_op=async_op)
        # print('Error happens')
        # print(type(output)) # torch.Tensor
        # print(type(output_b)) # 'torch.nn.parameter.Parameter' for bias
        # print(output.size())
        # print(output_b.size())
        # print(output.device) # cuda:0
        # print(output_b.device) # cuda:0
        return output + output_b

    @staticmethod
    def backward(ctx, grad_output):
        raise RuntimeError('You are running with DeepSpeed Inference mode. \
                            Please switch to Training mode for running backward!')


class DeepSpeedMoEMLP(nn.Module):
    def __init__(self,
                 config,
                 q_scales=None,
                 q_groups=1,
                 merge_count=1,
                 mlp_extra_grouping=False,
                 mp_group=None):
        super(DeepSpeedMoEMLP, self).__init__()

        self.config = config
        self.attn_nw = nn.Parameter(torch.Tensor(self.config.hidden_size))
        self.attn_nb = nn.Parameter(torch.Tensor(self.config.hidden_size))
        interm_size = self.config.intermediate_size // (
            1 if mp_group is None else dist.get_world_size(group=mp_group))
        self.inter_w = nn.Parameter(torch.Tensor(self.config.hidden_size, interm_size))
        self.inter_b = nn.Parameter(torch.Tensor(interm_size))
        self.output_w = nn.Parameter(torch.Tensor((interm_size),
                                                  self.config.hidden_size))
        self.output_b = nn.Parameter(torch.Tensor(self.config.hidden_size))

        # used for quantization
        self.q_scales = q_scales
        self.q_groups = q_groups * 2 if mlp_extra_grouping else q_groups
        self.merge_count = int(math.log2(merge_count))
        self.mp_group = mp_group

    def forward(self, input, async_op=False):
        return DeepSpeedMLPFunction.apply(input,
                                          self.inter_w,
                                          self.inter_b,
                                          self.config,
                                          self.output_b,
                                          self.output_w,
                                          self.q_scales,
                                          self.q_groups,
                                          self.merge_count,
                                          self.mp_group,
                                          async_op)


class DeepSpeedMoEInference(nn.Module):
    """Initialize the DeepSpeed MoE Transformer Layer.
        Arguments:
            layer_id: The layer index starting from 0, e.g. if model has 24 transformer layers,
                layer_id will be 0,1,2...23 when each layer object is instantiated
            config: An object of DeepSpeedInferenceConfig
            mp_group: Model parallelism group initialized on the modeling side.
            quantize_scales: This argument groups all the layers' scales used for quantization
            quantize_groups: Number of groups used for quantizing the model
            merge_count: Shows the number of model-parallel checkpoints merged before running inference.
                We use this argument to control the quantization scale for the model parameters if a bigger
                quantize-grouping than 1 is used.
            mlp_extra_grouping: This flag is used to show a 2x higher number of groups used for the MLP part
                of a Transformer layer. We use this feature for quantization to reduce the convergence impact
                for specific downstream tasks.
    """
    layer_id = 0

    def __init__(self,
                 config,
                 mp_group=None,
                 ep_group=None,
                 expert_mp_group=None,
                 quantize_scales=None,
                 quantize_groups=1,
                 merge_count=1,
                 mlp_extra_grouping=False,
                 qkv_merging=False):
        super(DeepSpeedMoEInference, self).__init__()

        self.config = config
        self.config.layer_id = DeepSpeedMoEInference.layer_id
        global inference_cuda_module
        global specialized_mode
        if inference_cuda_module is None:
            specialized_mode = False
            if hasattr(op_builder, 'InferenceSpecializedBuilder'):
                builder = op_builder.InferenceSpecializedBuilder()
                if builder.is_compatible():
                    inference_cuda_module = builder.load()
                    specialized_mode = True
                else:
                    inference_cuda_module = op_builder.InferenceBuilder().load()
            else:
                inference_cuda_module = op_builder.InferenceBuilder().load()
        self.config.specialized_mode = specialized_mode

        DeepSpeedMoEInference.layer_id += 1
        self.attention = DeepSpeedSelfAttention(self.config,
                                                mp_group,
                                                quantize_scales,
                                                quantize_groups,
                                                merge_count,
                                                qkv_merging)
        self.attn_nw = nn.Parameter(torch.Tensor(self.config.hidden_size))
        self.attn_nb = nn.Parameter(torch.Tensor(self.config.hidden_size))

        self.norm_w = nn.Parameter(torch.Tensor(self.config.hidden_size))
        self.norm_b = nn.Parameter(torch.Tensor(self.config.hidden_size))

        if config.mlp_type == 'residual':
            self.res_mlp = DeepSpeedMoEMLP(config,
                                           quantize_scales,
                                           quantize_groups,
                                           merge_count,
                                           mlp_extra_grouping,
                                           mp_group)
            self.res_coef = nn.Parameter(torch.Tensor(self.config.hidden_size, 2))
            self.coef_func = inference_cuda_module.softmax_fp16 if self.config.fp16 or self.config.q_int8 else \
                                        inference_cuda_module.softmax_fp32
            self.vector_matmul_func = inference_cuda_module.vector_matmul_fp16 if config.fp16 else \
                                    inference_cuda_module.vector_matmul_fp32

        config.mp_size = 1
        self.mlp = nn.ModuleList(
            DeepSpeedMoEMLP(config,
                            quantize_scales,
                            quantize_groups,
                            merge_count,
                            mlp_extra_grouping,
                            expert_mp_group) for i in range(self.config.moe_experts))

        self.moe_gate = TopKGate(self.config.hidden_size,
                                 self.config.global_experts,
                                 self.config.k,
                                 self.config.capacity_factor,
                                 self.config.eval_capacity_factor,
                                 self.config.min_capacity,
                                 self.config.noisy_gate_policy,
                                 self.config.drop_tokens,
                                 self.config.use_rts)

        self.ep_group = ep_group
        self.mp_group = mp_group
        self.expert_mp_group = expert_mp_group

        print("DeepSpeed MoE Transformer Inference config is ", self.config.__dict__)

        self.bias_residual_func = inference_cuda_module.bias_residual_fp16 if config.fp16 or config.q_int8 else \
                                        inference_cuda_module.bias_residual_fp32
        self.ds_layernorm = inference_cuda_module.layer_norm_fp16 if self.config.fp16 or self.config.q_int8 else \
                                        inference_cuda_module.layer_norm_fp32
        self.einsum_sec_sm_ecm = inference_cuda_module.einsum_sec_sm_ecm_fp16 if self.config.fp16 or self.config.q_int8 else \
                                        inference_cuda_module.einsum_sec_sm_ecm_fp32

    def res_coef_func(self, inp, async_op):
        inp = self.vector_matmul_func(inp, self.res_coef, async_op)
        return self.coef_func(inp, torch.empty(1), False, False, False, 256, async_op)

    def moe_gate_einsum(self, attention_output):
        _, combined_weights, dispatch_mask, _ = self.moe_gate(
            attention_output.view(-1, self.config.hidden_size),
            None,
            )
        dispatched_attention = self.einsum_sec_sm_ecm(
            dispatch_mask.type_as(attention_output),
            attention_output.view(-1,
                                  self.config.hidden_size))
        return dispatched_attention, combined_weights

    def expert_exec(self, dispatched_input):
        dispatched_input = dispatched_input.reshape(
            self.config.global_experts // self.config.moe_experts,
            self.config.moe_experts,
            -1,
            self.config.hidden_size)

        chunks = dispatched_input.chunk(self.config.moe_experts, dim=1)
        expert_outputs = torch.empty((
            self.config.moe_experts,
            chunks[0].shape[0],
        ) + chunks[0].shape[2:],
                                     dtype=dispatched_input.dtype,
                                     device=dispatched_input.device)
        for chunk, expert in zip(chunks, range(len(self.mlp))):
            # This is still a serialized execution -- why?
            expert_outputs[expert] = self.mlp[expert](chunk.view(
                -1,
                dispatched_input.shape[-2],
                dispatched_input.shape[-1]))
        return expert_outputs

    def _alltoall(self, dispatched_attention):
        if dist.get_world_size(group=self.ep_group) > 1:
            dispatched_attention = dispatched_attention.contiguous() # HH: a fix from fairseq
            dispatched_input = torch.empty_like(dispatched_attention) # HH: a fix from fairseq
            print_("Prepared for output") # This line triggered but all to all is problematic
            print_(f"Type: {dispatched_attention.type()}, Size: {dispatched_attention.size()}")
            print(get_nvidia_smi_gpu_memory_stats_str())
            dist.barrier() # HH: FIXME for debug purpose, synchronize all processes
            dist.all_to_all_single(dispatched_input,
                                   dispatched_attention,
                                   group=self.ep_group)
            # dispatched_input = dispatched_attention
            print_("All to all finished") # Halted here.
            # NCCL backend may hang/crash when there are concurrent communication
            # from different NCCL communicators
            return dispatched_input
        else:
            return dispatched_attention

    def scale_expert_output(self, attention_output, expert_output, combined_weights):
        combined_output = torch.matmul(
            combined_weights.type_as(attention_output).reshape(
                combined_weights.shape[0],
                -1),
            expert_output.reshape(-1,
                                  expert_output.shape[-1]))
        return combined_output.reshape(attention_output.shape)

    def forward(self,
                input,
                input_mask=None,
                attention_mask=None,
                head_mask=None,
                layer_past=None,
                get_key_value=False,
                get_present=False,
                encoder_output=None,
                enc_dec_attn_mask=None,
                encoder_hidden_states=None,
                encoder_attention_mask=None,
                use_cache=False,
                output_attentions=False):
        get_present = (get_present or get_key_value or use_cache)
        input_mask = input_mask if attention_mask is None else attention_mask
        input_type = input.dtype

        print_(f"This is the {self.config.layer_id}-th layer. This is an MoE layer.")
        print_(f"{self.config.layer_id}-th layer: First 3 rows of MoE input is {input[:3]}")

        if (self.config.fp16 or self.config.q_int8) \
            and input.dtype == torch.float:
            input = input.half()

        with torch.no_grad():
            attention_output = self.attention(input,
                                              input_mask,
                                              head_mask,
                                              layer_past,
                                              get_present,
                                              encoder_hidden_states,
                                              encoder_attention_mask,
                                              output_attentions,
                                              self.norm_w,
                                              self.norm_b)

            if get_present:
                attention_output, p_key, p_value = attention_output[0:3]
                presents = (p_key, p_value)
            elif output_attentions:
                attention_output, _, _, context_output = attention_output[0:4]
            else:
                attention_output = attention_output[0]

            print_('Attention finished')
            # print("Before error:", get_nvidia_smi_gpu_memory_stats_str())
            # print(type(attention_output))
            # print(type(self.attention.attn_ob))
            # print(attention_output.size())
            # print(self.attention.attn_ob.size())
            # print(attention_output.data_ptr()) # cuda:0
            # print(self.attention.attn_ob.data_ptr()) # cuda:0
            # print(f"{self.config.layer_id}-th layer: First three items in MoE attn:",(self.attention.attn_ob + attention_output)[:3])
            # print(get_nvidia_smi_gpu_memory_stats_str())
            residual_add = attention_output + self.attention.attn_ob
            attention_output = self.ds_layernorm(residual_add,
                                                 self.attn_nw,
                                                 self.attn_nb,
                                                 self.config.epsilon)
            print_('Layernorm finished')
            if self.config.mlp_type == 'residual':
                # print("PRMOE Triggered -- Should not happen") # Not being called
                res_mlp_out = self.res_mlp(attention_output, async_op=True)
                res_coef_out = self.res_coef_func(attention_output, async_op=True)

            if self.expert_mp_group is not None:
                # print("Expert MP group not found -- No Expert Parallelism.") # Not being called
                tensor_list = [
                    torch.empty_like(attention_output)
                    for _ in range(dist.get_world_size(group=self.expert_mp_group))
                ]
                tensor_list[dist.get_rank(group=self.expert_mp_group)] = attention_output
                dist.all_gather(tensor_list,
                                attention_output,
                                group=self.expert_mp_group)
                attention_output = torch.cat(tensor_list).contiguous()

            ############## MoE Gating + Experts ###############
            dispatched_attention, combined_weights = self.moe_gate_einsum(attention_output)
            print('Gating computed')
            print_(f"The dispatched attention of size {dispatched_attention.size()} is {dispatched_attention}")
            print_(f"The gating output of size {combined_weights.size()} is {combined_weights.sum(dim=2)}\n")
            dispatched_input = self._alltoall(dispatched_attention)
            print_(f'Tokens distributed: {dispatched_attention.size()}')
            expert_outputs = self.expert_exec(dispatched_input)
            print_(f'Experts executed')
            expert_output = self._alltoall(expert_outputs)
            print_(f'Tokens collected: {expert_output.size()}')
            # print(f"{self.config.layer_id}-th layer: "\
            #     + f"The gating output of size {combined_weights.size()} is {combined_weights}\n"\
            #     + f"The expert output of size {expert_output.size()} is {expert_output[:3]} \n") # Normal stuff
            output = self.scale_expert_output(attention_output,
                                              expert_output,
                                              combined_weights)
            print_(f'Experts output scaled')
            ################################################
            # print(f"{self.config.layer_id}-th layer: First 3 rows of MoE output after the scale is {output[:3]}") # Here we got these zeros!

            if self.expert_mp_group is not None:
                output = output.split(output.shape[0] //
                                      dist.get_world_size(group=self.expert_mp_group),
                                      dim=0)[dist.get_rank(group=self.expert_mp_group)]
            # print(f"{self.config.layer_id}-th layer: First 3 rows of MoE output before bias and residual is {output[:3]}") # Here we got these zeros!

            if self.config.mlp_type == 'residual':
                inference_cuda_module.moe_res_matmul(res_mlp_out, res_coef_out, output)

            output = self.bias_residual_func(output, residual_add, torch.empty(1))

            if not self.config.pre_layer_norm:
                output = self.ds_layernorm(output,
                                           self.norm_w,
                                           self.norm_b,
                                           self.config.epsilon)

            if input_type != output.dtype:
                output = output.to(input_type)

        print_(f"{self.config.layer_id}-th layer: NaN present? {torch.isnan(output).any()} First 3 rows of MoE output is {output[:3]}")
        if get_present:
            output = (output, presents)

        if self.config.return_tuple:
            return output if type(output) is tuple else (output, )
        else:
            return output

def get_nvidia_smi_gpu_memory_stats_str():
    return f"nvidia-smi stats: ALLOC: {torch.cuda.memory_allocated()/1024/1024/1024:.3f}GB, RESERVED: {torch.cuda.memory_reserved()/1024/1024/1024:.3f}GB"

def print_(content, *args, **kwargs):
    torch.cuda.synchronize()
    print(content, *args, **kwargs)
