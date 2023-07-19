# Copyright 2022 Cerebras Systems.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import re
from typing import Tuple

import torch

from modelzoo.common.pytorch.model_utils.checkpoint_converters.base_converter import (
    BaseCheckpointConverter_HF_CS,
    BaseConfigConverter,
    BaseConfigConverter_HF_CS,
    ConversionRule,
    EquivalentSubkey,
    FormatVersions,
)


class Converter_GPT_Neox_Attention_HF_CS17(BaseCheckpointConverter_HF_CS):
    def __init__(self):
        super().__init__()
        self.rules = [
            ConversionRule(
                [
                    EquivalentSubkey("dense", "proj_output_dense_layer"),
                    "\.(?:weight|bias)",
                ],
                action=self.replaceKey,
            ),
            ConversionRule(
                [
                    EquivalentSubkey("query_key_value", "proj_q_dense_layer"),
                    "\.(?:weight|bias)",
                ],
                action=self.qkv_converter,
            ),
            ConversionRule(
                [
                    EquivalentSubkey("query_key_value", "proj_k_dense_layer"),
                    "\.(?:weight|bias)",
                ],
                action=self.assert_already_converted,
            ),
            ConversionRule(
                [
                    EquivalentSubkey("query_key_value", "proj_v_dense_layer"),
                    "\.(?:weight|bias)",
                ],
                action=self.assert_already_converted,
            ),
        ]

    @staticmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        return (FormatVersions("hf"), FormatVersions("cs-1.7"))

    @staticmethod
    def get_config_converter_class() -> BaseConfigConverter:
        return None

    def interleave_helper(self, t, cs_config):
        rotary_dim = cs_config["model"]["rotary_dim"]
        if len(t.shape) == 3:
            to_rotate = t[:, :rotary_dim, :]
            to_pass = t[:, rotary_dim:, :]
            to_rotate = (
                to_rotate.reshape(t.shape[0], 2, -1, t.shape[-1])
                .permute(0, 2, 1, 3)
                .reshape(t.shape[0], -1, t.shape[-1])
            )
            interleaved = torch.cat((to_rotate, to_pass), dim=1)
        elif len(t.shape) == 2:
            to_rotate = t[:, :rotary_dim]
            to_pass = t[:, rotary_dim:]
            to_rotate = (
                to_rotate.reshape(t.shape[0], 2, -1)
                .permute(0, 2, 1)
                .reshape(t.shape[0], -1)
            )
            interleaved = torch.cat((to_rotate, to_pass), dim=1)
        else:
            assert (
                False
            ), "shape of query, key, value projection tensor has to have shape of length 2 (biases) or 3 (weights) when converting from HF to CS"
        return interleaved

    def reverse_interleave_helper(self, t, cs_config, num_heads=None):
        if num_heads is None:
            num_heads = cs_config["model"]["num_heads"]
        rotary_dim = cs_config["model"]["rotary_dim"]
        if len(t.shape) == 2:
            t = t.reshape(num_heads, -1, t.shape[-1])
            to_rotate = t[:, :rotary_dim, :]
            to_pass = t[:, rotary_dim:, :]
            reversed = (
                to_rotate.reshape(num_heads, -1, 2, t.shape[-1])
                .permute(0, 2, 1, 3)
                .reshape(num_heads, rotary_dim, t.shape[-1])
            )
            reversed = torch.cat((reversed, to_pass), dim=1)
        elif len(t.shape) == 1:
            t = t.reshape(num_heads, -1)
            to_rotate = t[:, :rotary_dim]
            to_pass = t[:, rotary_dim:]
            reversed = (
                to_rotate.reshape(num_heads, -1, 2)
                .permute(0, 2, 1)
                .reshape(num_heads, -1)
            )
            reversed = torch.cat((reversed, to_pass), dim=1)
        else:
            assert (
                False
            ), "shape of query, key, value projection tensor has to have shape of length 1 (biases) or 2 (weights) when converting from CS to HF"
        return reversed

    def qkv_converter(
        self,
        old_key,
        new_key,
        old_state_dict,
        new_state_dict,
        from_index,
        action_fn_args,
    ):
        if from_index == 0:
            self.qkv_converter_hf_to_cs17(
                old_key, new_key, old_state_dict, new_state_dict, action_fn_args
            )
        else:
            self.qkv_converter_cs17_to_hf(
                old_key, new_key, old_state_dict, new_state_dict, action_fn_args
            )

    def qkv_converter_hf_to_cs17(
        self, old_key, new_key, old_state_dict, new_state_dict, action_fn_args
    ):
        # HF represents Q, K, and V in a packed format (torch.Size(3*hidden, hidden)). We need to unpack the
        # weight and bias tensor for CS 1.7 format.
        q_key = new_key
        k_key = re.sub("\.proj_q_dense_layer\.", ".proj_k_dense_layer.", q_key)
        v_key = re.sub("\.proj_q_dense_layer\.", ".proj_v_dense_layer.", q_key)

        cs_config = action_fn_args["configs"][1]
        num_heads = cs_config["model"]["num_heads"]

        if new_key.endswith(".bias"):
            assert len(old_state_dict[old_key].shape) == 1
            packed_dim = old_state_dict[old_key].shape[0]
            embed_dim = packed_dim // 3
            head_size = embed_dim // num_heads
            assert (
                3 * embed_dim == packed_dim
            ), "Invalid tensor shape {} at {}. Bias should be divisible by 3 since Q, K, and V are packed".format(
                old_state_dict[old_key].shape, old_key
            )
            split_by_num_heads = old_state_dict[old_key].reshape(num_heads, -1)
            query, key, value = torch.split(
                split_by_num_heads, head_size, dim=1
            )

            query = self.interleave_helper(query, cs_config)
            key = self.interleave_helper(key, cs_config)

            query = query.reshape(-1)
            value = value.reshape(-1)
            key = key.reshape(-1)
            new_state_dict[q_key] = query
            new_state_dict[k_key] = key
            new_state_dict[v_key] = value
        elif new_key.endswith(".weight"):
            packed_dim, dim = old_state_dict[old_key].shape
            head_size = dim // num_heads
            assert (
                3 * dim == packed_dim
            ), "Invalid tensor shape {} at {}. The first dimension (packed_dim) should be 3x the second dimension (embed_dim) since Q, K, and V are packed".format(
                old_state_dict[old_key].shape, old_key
            )
            split_by_num_heads = old_state_dict[old_key].reshape(
                num_heads, -1, dim
            )
            query, key, value = torch.split(
                split_by_num_heads, head_size, dim=1
            )

            query = self.interleave_helper(query, cs_config)
            key = self.interleave_helper(key, cs_config)

            query = query.reshape(-1, dim)
            value = value.reshape(-1, dim)
            key = key.reshape(-1, dim)
            new_state_dict[q_key] = query
            new_state_dict[k_key] = key
            new_state_dict[v_key] = value
        else:
            raise ValueError("Invalid key after conversion: {}".format(new_key))

    def qkv_converter_cs17_to_hf(
        self, old_key, new_key, old_state_dict, new_state_dict, action_fn_args
    ):
        # HF represents Q, K, and V in a packed format. It also contains
        # special ".bias" and ".masked_bias" register buffers that need to be
        # initalized
        q_key = old_key
        k_key = re.sub("\.proj_q_dense_layer\.", ".proj_k_dense_layer.", q_key)
        v_key = re.sub("\.proj_q_dense_layer\.", ".proj_v_dense_layer.", q_key)

        assert (
            k_key in old_state_dict
        ), "Expected the following key to exist! {}".format(k_key)
        assert (
            v_key in old_state_dict
        ), "Expected the following key to exist! {}".format(v_key)

        query = old_state_dict[q_key]
        value = old_state_dict[v_key]
        key = old_state_dict[k_key]

        if new_key.endswith(".bias"):
            cs_config = action_fn_args["configs"][1]
            max_positions = cs_config["model"]["max_position_embeddings"]
            rotary_dim = cs_config["model"]["rotary_dim"]
            num_heads = cs_config["model"]["num_heads"]
            hf_config = action_fn_args["configs"][0]
            rotary_emb_base = hf_config["rotary_emb_base"]

            # map qkv
            query = self.reverse_interleave_helper(query, cs_config)
            key = self.reverse_interleave_helper(key, cs_config)
            value = value.reshape(num_heads, -1)

            packed_qkv = torch.cat((query, key, value,), dim=-1,)
            packed_qkv = packed_qkv.reshape(-1)
            new_state_dict[new_key] = packed_qkv

            # build model params that don't exist in CS models
            attn_bias_key = re.sub("\.query_key_value\.", ".", new_key)
            new_state_dict[attn_bias_key] = torch.tril(
                torch.ones((max_positions, max_positions), dtype=torch.uint8)
            ).view(1, 1, max_positions, max_positions)

            masked_bias_key = re.sub("\.query_key_value\.", ".masked_", new_key)
            new_state_dict[masked_bias_key] = torch.tensor(-1e9)

            inv_freq_key = re.sub(
                "\.query_key_value\.bias", ".rotary_emb.inv_freq", new_key
            )
            new_state_dict[inv_freq_key] = 1.0 / (
                rotary_emb_base
                ** (torch.arange(0, rotary_dim, 2).float() / rotary_dim)
            )
        elif new_key.endswith(".weight"):
            num_heads = action_fn_args["configs"][1]["model"]["num_heads"]
            hidden_size = query.shape[-1]
            query = self.reverse_interleave_helper(
                query, action_fn_args["configs"][1]
            )
            key = self.reverse_interleave_helper(
                key, action_fn_args["configs"][1]
            )
            value = value.reshape(num_heads, -1, value.shape[-1])

            packed_qkv = torch.cat((query, key, value,), dim=1,)
            packed_qkv = packed_qkv.reshape(-1, hidden_size)
            new_state_dict[new_key] = packed_qkv
        else:
            raise ValueError("Invalid key after conversion: {}".format(new_key))

    def assert_already_converted(
        self,
        old_key,
        new_key,
        old_state_dict,
        new_state_dict,
        from_index,
        action_fn_args,
    ):
        if from_index == 0:
            # We should never hit this case as this key should have been matched
            # already
            assert False, "Invalid key: {}".format(old_key)
        else:
            # When we convert from CS -> HF, the proj_q_dense_layer should also handle
            # conversion of proj_k_dense_layer and proj_v_dense_layer since HF
            # represents these three layers in a packed format. We simply need
            # to test that the key containing the packed format has already
            # been converted.
            assert (
                new_key in new_state_dict
            ), "Key should've been already converted: {} -> {}".format(
                old_key, new_key
            )


class Converter_GPT_Neox_Headless_HF_CS17(BaseCheckpointConverter_HF_CS):
    def __init__(self):
        super().__init__()
        self.rules = [
            # embedding
            ConversionRule(
                [
                    EquivalentSubkey(
                        "embed_in", "embedding_layer.word_embeddings"
                    ),
                    "\.(?:weight|bias)",
                ],
                action=self.replaceKey,
            ),
            # final layer norm
            ConversionRule(
                [
                    EquivalentSubkey(
                        "final_layer_norm", "transformer_decoder.norm"
                    ),
                    "\.(?:weight|bias)",
                ],
                action=self.replace_final_norm,
            ),
            # attention
            ConversionRule(
                [
                    EquivalentSubkey("layers", "transformer_decoder.layers"),
                    "\.\d+\.",
                    EquivalentSubkey("attention.", "self_attn."),
                    Converter_GPT_Neox_Attention_HF_CS17(),
                ],
                action=None,
            ),
            # 2 layernorms
            ConversionRule(
                [
                    EquivalentSubkey("layers", "transformer_decoder.layers"),
                    "\.\d+\.",
                    EquivalentSubkey("input_layernorm", "norm1"),
                    "\.(?:weight|bias)",
                ],
                action=self.replaceKey,
            ),
            ConversionRule(
                [
                    EquivalentSubkey("layers", "transformer_decoder.layers"),
                    "\.\d+\.",
                    EquivalentSubkey("post_attention_layernorm", "norm3"),
                    "\.(?:weight|bias)",
                ],
                action=self.replaceKey,
            ),
            # ffn
            ConversionRule(
                [
                    EquivalentSubkey("layers", "transformer_decoder.layers"),
                    "\.\d+\.",
                    EquivalentSubkey(
                        "mlp.dense_h_to_4h", "ffn.ffn.0.linear_layer"
                    ),
                    "\.(?:weight|bias)",
                ],
                action=self.replaceKey,
            ),
            ConversionRule(
                [
                    EquivalentSubkey("layers", "transformer_decoder.layers"),
                    "\.\d+\.",
                    EquivalentSubkey(
                        "mlp.dense_4h_to_h", "ffn.ffn.1.linear_layer"
                    ),
                    "\.(?:weight|bias)",
                ],
                action=self.replaceKey,
            ),
            # others
            ConversionRule(["lm_head\.(?:weight|bias)"], exists="right"),
            ConversionRule(["ln_f\.(?:weight|bias)"], exists="right"),
            ConversionRule(
                ["layers\.\d+\.attention\.rotary_emb\.inv_freq"], exists="left"
            ),
            ConversionRule(
                ["layers\.\d+\.attention\.(?:masked_bias|bias)",], exists="left"
            ),
        ]

    def replace_final_norm(
        self,
        old_key,
        new_key,
        old_state_dict,
        new_state_dict,
        from_index,
        action_fn_args,
    ):
        new_state_dict[new_key] = old_state_dict[old_key]
        # CS 1.7 has both "ln_f" and "transformer_decoder.norm"
        # we need to copy the original ("ln_f") too:
        if from_index == 0:
            ln_f_key = re.sub("transformer_decoder\.norm\.", "ln_f.", new_key)
            new_state_dict[ln_f_key] = old_state_dict[old_key]

    def pre_model_convert(
        self,
        old_state_dict,
        new_state_dict,
        configs,
        from_index,
        drop_unmatched_keys,
    ):
        if from_index == 0:
            logging.warning(
                "{} GPT Neox has a language model head (lm_head) "
                "while {} GPTNeoxModel does not. Initializing lm_head to default.".format(
                    self.formats()[1], self.formats()[0]
                )
            )

        # Manually tie weights
        if from_index == 1 and configs[1]["model"]["share_embedding_weights"]:
            if (
                old_state_dict.get("embedding_layer.word_embeddings.weight", 0)
                is None
            ):
                old_state_dict[
                    "embedding_layer.word_embeddings.weight"
                ] = old_state_dict["lm_head.weight"]

    def post_model_convert(
        self,
        old_state_dict,
        new_state_dict,
        configs,
        from_index,
        drop_unmatched_keys,
    ):
        if from_index == 0:
            # We are converting from HF GPTNeoxModel (which is headless) -> CS GPTNeoxModel (which has a head)
            # We need to create 'lm_head' and init to default values
            hf_config = configs[0]
            cs_config = configs[1]
            use_bias_in_output = cs_config["model"].get(
                "use_bias_in_output", False
            )
            vocab_size = cs_config["model"]["vocab_size"]
            embed_dim = cs_config["model"]["hidden_size"]
            if hf_config["tie_word_embeddings"]:
                lm_head_weight = old_state_dict['embed_in.weight']
            else:
                lm_head_weight = torch.zeros((vocab_size, embed_dim))
                lm_head_weight.normal_(mean=0.0, std=0.02)
            new_state_dict["lm_head.weight"] = lm_head_weight
            if use_bias_in_output:
                lm_head_bias = torch.zeros(vocab_size)
                new_state_dict["lm_head.bias"] = lm_head_bias

    @staticmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        return (FormatVersions("hf"), FormatVersions("cs-1.7"))

    @classmethod
    def converter_note(cls) -> str:
        return (
            "{} GPTNeoXForCausalLM <-> {} GPTJModel (configured as neox)\n"
            "The HF model doesn't contain a language model head while the CS "
            "one does. When converting to CS, the exported checkpoint will "
            "contain a language model head initialized to default random "
            "values. When converting to HF, the language model head will be "
            "dropped."
        ).format(cls.formats()[0], cls.formats()[1])

    @staticmethod
    def get_config_converter_class() -> BaseConfigConverter:
        return ConfigConverter_GPT_Neox_HF_CS17


class Converter_GPT_Neox_Headless_HF_CS18(Converter_GPT_Neox_Headless_HF_CS17):
    def __init__(self):
        super().__init__()
        self.rules = [
            # Catch checkpoints from Pytorch 2.0 API
            ConversionRule(
                [Converter_GPT_Neox_Headless_HF_CS17(),], action=None,
            ),
            # Catch checkpoints from depricated PyTorchBaseModel
            ConversionRule(
                [
                    EquivalentSubkey("", "model."),
                    Converter_GPT_Neox_Headless_HF_CS17(),
                ],
                action=None,
            ),
        ]

    @staticmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        return (FormatVersions("hf"), FormatVersions("cs-1.8", "cs-1.9"))

    @classmethod
    def converter_note(cls) -> str:
        return (
            "{} GPTNeoXForCausalLM <-> {} GPTJModel (configured as neox)\n"
            "The HF model doesn't contain a language model head while the CS "
            "one does. When converting to CS, the exported checkpoint will "
            "contain a language model head initialized to default random "
            "values. When converting to HF, the language model head will be "
            "dropped."
        ).format(cls.formats()[0], cls.formats()[1])

    @staticmethod
    def get_config_converter_class() -> BaseConfigConverter:
        return ConfigConverter_GPT_Neox_HF_CS18


class Converter_GPT_Neox_LMHeadModel_HF_CS17(BaseCheckpointConverter_HF_CS):
    def __init__(self):
        super().__init__()
        self.rules = [
            ConversionRule(
                [
                    EquivalentSubkey("embed_out", "lm_head"),
                    "\.(?:weight|bias)",
                ],
                action=self.replaceKey,
            ),
            ConversionRule(
                [
                    EquivalentSubkey("gpt_neox.", ""),
                    Converter_GPT_Neox_Headless_HF_CS17(),
                ],
                action=None,
            ),
        ]

    def pre_model_convert(
        self,
        old_state_dict,
        new_state_dict,
        configs,
        from_index,
        drop_unmatched_keys,
    ):
        # Manually tie weights
        if from_index == 1 and configs[1]["model"]["share_embedding_weights"]:
            if (
                old_state_dict.get("embedding_layer.word_embeddings.weight", 0)
                is None
            ):
                old_state_dict[
                    "embedding_layer.word_embeddings.weight"
                ] = old_state_dict["lm_head.weight"]

    @staticmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        return (FormatVersions("hf"), FormatVersions("cs-1.7"))

    @classmethod
    def converter_note(cls) -> str:
        return "{} GPTNeoXForCausalLM <-> {} GPTJModel (configured as neox) with LM head".format(
            cls.formats()[0], cls.formats()[1]
        )

    @staticmethod
    def get_config_converter_class() -> BaseConfigConverter:
        return ConfigConverter_GPT_Neox_HF_CS17


class Converter_GPT_Neox_LMHeadModel_HF_CS18(BaseCheckpointConverter_HF_CS):
    def __init__(self):
        super().__init__()
        self.rules = [
            # Catch checkpoints from Pytorch 2.0 API
            ConversionRule(
                [Converter_GPT_Neox_LMHeadModel_HF_CS17(),], action=None,
            ),
            # Catch checkpoints from depricated PyTorchBaseModel
            ConversionRule(
                [
                    EquivalentSubkey("", "model."),
                    Converter_GPT_Neox_LMHeadModel_HF_CS17(),
                ],
                action=None,
            ),
        ]

    @staticmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        return (FormatVersions("hf"), FormatVersions("cs-1.8", "cs-1.9"))

    @classmethod
    def converter_note(cls) -> str:
        return "{} GPTNeoXForCausalLM <-> {} GPTJModel (configured as neox) with LM head".format(
            cls.formats()[0], cls.formats()[1]
        )

    @staticmethod
    def get_config_converter_class() -> BaseConfigConverter:
        return ConfigConverter_GPT_Neox_HF_CS18


class ConfigConverter_GPT_Neox_HF_CS17(BaseConfigConverter_HF_CS):
    def __init__(self):
        super().__init__()
        self.rules = [
            # Embedding
            ConversionRule(["vocab_size"], action=self.replaceKey),
            ConversionRule(
                [EquivalentSubkey("rotary", "position_embedding_type")],
                action=BaseConfigConverter.assert_factory_fn(1, "rotary"),
            ),
            ConversionRule(
                ["use_position_embedding"],
                exists="right",
                action=BaseConfigConverter.assert_factory_fn(1, True),
            ),
            ConversionRule(
                ["embedding_dropout_rate"],
                action=BaseConfigConverter.assert_factory_fn(1, 0.0),
            ),
            ConversionRule(
                [
                    EquivalentSubkey(
                        "tie_word_embeddings", "share_embedding_weights"
                    )
                ],
                action=self.replaceKey,
            ),
            # Decoder Block
            ConversionRule(["hidden_size"], action=self.replaceKey,),
            ConversionRule(
                [EquivalentSubkey("num_attention_heads", "num_heads")],
                action=self.replaceKey,
            ),
            ConversionRule(["num_hidden_layers"], action=self.replaceKey,),
            ConversionRule(
                ["max_position_embeddings"], action=self.replaceKey,
            ),
            ConversionRule(
                ["scale_attn_weights"],
                action=BaseConfigConverter.assert_factory_fn(0, True),
            ),
            ConversionRule(
                ["attention_type"],
                action=BaseConfigConverter.assert_factory_fn(
                    1, "scaled_dot_product"
                ),
            ),
            ConversionRule(
                ["use_projection_bias_in_attention"],
                action=BaseConfigConverter.assert_factory_fn(1, True),
            ),
            ConversionRule(
                ["use_ffn_bias_in_attention"],
                exists="right",
                action=BaseConfigConverter.assert_factory_fn(1, True),
            ),
            ConversionRule(
                ["use_ffn_bias"],
                exists="right",
                action=BaseConfigConverter.assert_factory_fn(1, True),
            ),
            ConversionRule(
                [EquivalentSubkey("intermediate_size", "filter_size")],
                action=self.replaceKey,
            ),
            ConversionRule(
                [EquivalentSubkey("hidden_act", "nonlinearity")],
                action=self.replaceKey,
            ),
            ConversionRule(
                [EquivalentSubkey("layer_norm_eps", "layer_norm_epsilon")],
                action=self.replaceKey,
            ),
            ConversionRule(
                ["attention_dropout_rate"],
                action=BaseConfigConverter.assert_factory_fn(1, 0.0),
            ),
            ConversionRule(
                ["residual_dropout_rate"],
                action=BaseConfigConverter.assert_factory_fn(1, 0.0),
            ),
            ConversionRule(
                [EquivalentSubkey("rotary_pct", "rotary_dim")],
                action=self.rotary_dim_converter,
            ),
            ConversionRule(
                ["rotary_emb_base"],
                action=BaseConfigConverter.assert_factory_fn(0, 10000),
            ),
            ConversionRule(
                ["use_bias_in_output"],
                action=BaseConfigConverter.assert_factory_fn(1, False),
            ),
            ConversionRule(
                ["use_parallel_residual"],
                action=BaseConfigConverter.assert_factory_fn(0, True),
            ),
            ConversionRule(["initializer_range"], action=self.replaceKey),
            ConversionRule(
                ["embedding_layer_norm"],
                action=BaseConfigConverter.assert_factory_fn(1, False),
            ),
            ConversionRule(
                ["fixed_sparse_attention"],
                action=BaseConfigConverter.assert_factory_fn(1, None),
            ),
            ConversionRule(
                ["norm_first"],
                action=BaseConfigConverter.assert_factory_fn(1, True),
            ),
            ConversionRule(
                ["use_ff_layer1_dropout"],
                action=BaseConfigConverter.assert_factory_fn(1, False),
            ),
            ConversionRule(
                ["use_untied_layer_norm"],
                action=BaseConfigConverter.assert_factory_fn(1, True),
            ),
        ]

    def rotary_dim_converter(
        self,
        old_key,
        new_key,
        old_state_dict,
        new_state_dict,
        from_index,
        action_fn_args,
    ):
        if from_index == 0:
            new_state_dict[new_key] = int(
                (
                    old_state_dict["hidden_size"]
                    // old_state_dict["num_attention_heads"]
                )
                * old_state_dict[old_key]
            )
        else:
            head_size = (
                old_state_dict["hidden_size"] // old_state_dict["num_heads"]
            )
            new_state_dict[new_key] = old_state_dict[old_key] / head_size

    def pre_config_convert(
        self, config, from_index,
    ):
        config = super().pre_config_convert(config, from_index)

        defaults = [
            {
                "vocab_size": 50432,
                "hidden_size": 6144,
                "num_hidden_layers": 44,
                "num_attention_heads": 64,
                "intermediate_size": 24576,
                "hidden_act": "gelu",
                "rotary_pct": 0.25,
                "rotary_emb_base": 10000,
                "max_position_embeddings": 2048,
                "initializer_range": 0.02,
                "layer_norm_eps": 1e-5,
                "tie_word_embeddings": False,
                "use_parallel_residual": True,
            },
            {
                "max_position_embeddings": 1024,
                "embedding_dropout_rate": 0.1,
                "share_embedding_weights": True,
                "residual_dropout_rate": 0.1,
                "nonlinearity": "gelu",
                "layer_norm_epsilon": 1.0e-5,
                "use_ffn_bias": False,
                "use_untied_layer_norm": False,
                "attention_dropout_rate": 0.1,
                "use_projection_bias_in_attention": True,
                "use_ffn_bias_in_attention": True,
                "initializer_range": 0.02,
                "use_bias_in_output": False,
                "norm_first": True,
            },
        ]

        # Apply defaults
        for key in defaults[from_index]:
            if key not in config:
                config[key] = defaults[from_index][key]
        return config

    def post_config_convert(
        self,
        original_config,
        old_config,
        new_config,
        from_index,
        drop_unmatched_keys,
    ):
        if from_index == 0:
            new_config["attention_type"] = "scaled_dot_product"
            new_config["use_untied_layer_norm"] = True
            new_config["use_projection_bias_in_attention"] = True
            new_config["use_ffn_bias_in_attention"] = True
            new_config["use_ffn_bias"] = True
            new_config["use_bias_in_output"] = False
            new_config["embedding_dropout_rate"] = 0.0
            new_config["residual_dropout_rate"] = 0.0
            new_config["attention_dropout_rate"] = 0.0

        elif from_index == 1:
            if "rotary_pct" not in new_config:
                new_config["rotary_pct"] = 1.0
            if "rotary_emb_base" not in new_config:
                new_config["rotary_emb_base"] = 10000

        return super().post_config_convert(
            original_config,
            old_config,
            new_config,
            from_index,
            drop_unmatched_keys,
        )

    @staticmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        return (FormatVersions("hf"), FormatVersions("cs-1.7"))


class ConfigConverter_GPT_Neox_HF_CS18(ConfigConverter_GPT_Neox_HF_CS17):
    def __init__(self):
        super().__init__()

    @staticmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        return (FormatVersions("hf"), FormatVersions("cs-1.8", "cs-1.9"))
