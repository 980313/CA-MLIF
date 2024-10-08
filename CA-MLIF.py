import warnings

import torch
from torch import nn, einsum
import torch.nn.functional as F
from einops import rearrange, reduce
from math import ceil

from typing import Optional
from torch.nn.init import xavier_uniform_
from torch.nn.init import constant_
from torch.nn.init import xavier_normal_
from torch.nn import Module
from torch.nn.parameter import Parameter
from torch.nn.modules.linear import NonDynamicallyQuantizableLinear as _LinearWithBias
from torch import Tensor
from torch.overrides import has_torch_function, handle_torch_function
from MLIF_fusion import BilinearFusion

################
# Network Utils
################
def define_net(args):
    net = None
    act = define_act_layer(act_type=args.act_type)

    if args.mode == "path":
        # net = PATHNet(args)
        net = PATHNet2(args)
    elif args.mode == "ra":
        net = RANet(args)
    elif args.mode == "path_TU":
        net = PATHNet_TU(args)
    elif args.mode == "path_PaEp":
        net = PATHNet_PaEp(args)
    elif args.mode == "path_PaSt":
        net = PATHNet_PaSt(args)
    elif args.mode == "path_PaNu":
        net = PATHNet_PaNu(args)
    elif args.mode == "rapath":
        net = TrCross(args)
    elif args.mode == "pathomic":
        net = PathomicNet(args)
    return net

def define_act_layer(act_type='Tanh'):
    if act_type == 'Tanh':
        act_layer = nn.Tanh()
    elif act_type == 'ReLU':
        act_layer = nn.ReLU()
    elif act_type == 'Sigmoid':
        act_layer = nn.Sigmoid()
    elif act_type == 'LSM':
        act_layer = nn.LogSoftmax(dim=1)
    elif act_type == "none":
        act_layer = None
    else:
        raise NotImplementedError('activation layer [%s] is not found' % act_type)
    return act_layer

def define_bifusion(fusion_type, skip=1, use_bilinear=1, gate1=1, gate2=1, dim1=32, dim2=32, scale_dim1=1, scale_dim2=1, mmhid=64, dropout_rate=0.25):
    fusion = None
    if fusion_type == 'pofusion':
        fusion = BilinearFusion(skip=skip, use_bilinear=use_bilinear, gate1=gate1, gate2=gate2, dim1=dim1, dim2=dim2, scale_dim1=scale_dim1, scale_dim2=scale_dim2, mmhid=mmhid, dropout_rate=dropout_rate)
    else:
        raise NotImplementedError('fusion type [%s] is not found' % fusion_type)
    return fusion

def multi_head_attention_forward(
    query: Tensor,
    key: Tensor,
    value: Tensor,
    embed_dim_to_check: int,
    num_heads: int,
    in_proj_weight: Tensor,
    in_proj_bias: Tensor,
    bias_k: Optional[Tensor],
    bias_v: Optional[Tensor],
    add_zero_attn: bool,
    dropout_p: float,
    out_proj_weight: Tensor,
    out_proj_bias: Tensor,
    training: bool = True,
    key_padding_mask: Optional[Tensor] = None,
    need_weights: bool = True,
    need_raw: bool = True,
    attn_mask: Optional[Tensor] = None,
    use_separate_proj_weight: bool = False,
    q_proj_weight: Optional[Tensor] = None,
    k_proj_weight: Optional[Tensor] = None,
    v_proj_weight: Optional[Tensor] = None,
    static_k: Optional[Tensor] = None,
    static_v: Optional[Tensor] = None,
):
    r"""
    Args:
        query, key, value: map a query and a set of key-value pairs to an output.
            See "Attention Is All You Need" for more details.
        embed_dim_to_check: total dimension of the model.
        num_heads: parallel attention heads.
        in_proj_weight, in_proj_bias: input projection weight and bias.
        bias_k, bias_v: bias of the key and value sequences to be added at dim=0.
        add_zero_attn: add a new batch of zeros to the key and
                       value sequences at dim=1.
        dropout_p: probability of an element to be zeroed.
        out_proj_weight, out_proj_bias: the output projection weight and bias.
        training: apply dropout if is ``True``.
        key_padding_mask: if provided, specified padding elements in the key will
            be ignored by the attention. This is an binary mask. When the value is True,
            the corresponding value on the attention layer will be filled with -inf.
        need_weights: output attn_output_weights.
        attn_mask: 2D or 3D mask that prevents attention to certain positions. A 2D mask will be broadcasted for all
            the batches while a 3D mask allows to specify a different mask for the entries of each batch.
        use_separate_proj_weight: the function accept the proj. weights for query, key,
            and value in different forms. If false, in_proj_weight will be used, which is
            a combination of q_proj_weight, k_proj_weight, v_proj_weight.
        q_proj_weight, k_proj_weight, v_proj_weight, in_proj_bias: input projection weight and bias.
        static_k, static_v: static key and value used for attention operators.
    Shape:
        Inputs:
        - query: :math:`(L, N, E)` where L is the target sequence length, N is the batch size, E is
          the embedding dimension.
        - key: :math:`(S, N, E)`, where S is the source sequence length, N is the batch size, E is
          the embedding dimension.
        - value: :math:`(S, N, E)` where S is the source sequence length, N is the batch size, E is
          the embedding dimension.
        - key_padding_mask: :math:`(N, S)` where N is the batch size, S is the source sequence length.
          If a ByteTensor is provided, the non-zero positions will be ignored while the zero positions
          will be unchanged. If a BoolTensor is provided, the positions with the
          value of ``True`` will be ignored while the position with the value of ``False`` will be unchanged.
        - attn_mask: 2D mask :math:`(L, S)` where L is the target sequence length, S is the source sequence length.
          3D mask :math:`(N*num_heads, L, S)` where N is the batch size, L is the target sequence length,
          S is the source sequence length. attn_mask ensures that position i is allowed to attend the unmasked
          positions. If a ByteTensor is provided, the non-zero positions are not allowed to attend
          while the zero positions will be unchanged. If a BoolTensor is provided, positions with ``True``
          are not allowed to attend while ``False`` values will be unchanged. If a FloatTensor
          is provided, it will be added to the attention weight.
        - static_k: :math:`(N*num_heads, S, E/num_heads)`, where S is the source sequence length,
          N is the batch size, E is the embedding dimension. E/num_heads is the head dimension.
        - static_v: :math:`(N*num_heads, S, E/num_heads)`, where S is the source sequence length,
          N is the batch size, E is the embedding dimension. E/num_heads is the head dimension.
        Outputs:
        - attn_output: :math:`(L, N, E)` where L is the target sequence length, N is the batch size,
          E is the embedding dimension.
        - attn_output_weights: :math:`(N, L, S)` where N is the batch size,
          L is the target sequence length, S is the source sequence length.
    """
    tens_ops = (query, key, value, in_proj_weight, in_proj_bias, bias_k, bias_v, out_proj_weight, out_proj_bias)
    if has_torch_function(tens_ops):
        return handle_torch_function(
            multi_head_attention_forward,
            tens_ops,
            query,
            key,
            value,
            embed_dim_to_check,
            num_heads,
            in_proj_weight,
            in_proj_bias,
            bias_k,
            bias_v,
            add_zero_attn,
            dropout_p,
            out_proj_weight,
            out_proj_bias,
            training=training,
            key_padding_mask=key_padding_mask,
            need_weights=need_weights,
            need_raw=need_raw,
            attn_mask=attn_mask,
            use_separate_proj_weight=use_separate_proj_weight,
            q_proj_weight=q_proj_weight,
            k_proj_weight=k_proj_weight,
            v_proj_weight=v_proj_weight,
            static_k=static_k,
            static_v=static_v,
        )
    tgt_len, bsz, embed_dim = query.size()
    assert embed_dim == embed_dim_to_check
    assert key.size(0) == value.size(0) and key.size(1) == value.size(1)

    head_dim = embed_dim // num_heads
    assert head_dim * num_heads == embed_dim, "embed_dim must be divisible by num_heads"
    scaling = float(head_dim) ** -0.5

    if not use_separate_proj_weight:
        if (query is key or torch.equal(query, key)) and (key is value or torch.equal(key, value)):
            # self-attention
            q, k, v = F.linear(query, in_proj_weight, in_proj_bias).chunk(3, dim=-1)

        elif key is value or torch.equal(key, value):
            # encoder-decoder attention
            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = 0
            _end = embed_dim
            _w = in_proj_weight[_start:_end, :]
            if _b is not None:
                _b = _b[_start:_end]
            q = F.linear(query, _w, _b)

            if key is None:
                assert value is None
                k = None
                v = None
            else:
                # This is inline in_proj function with in_proj_weight and in_proj_bias
                _b = in_proj_bias
                _start = embed_dim
                _end = None
                _w = in_proj_weight[_start:, :]
                if _b is not None:
                    _b = _b[_start:]
                k, v = F.linear(key, _w, _b).chunk(2, dim=-1)

        else:
            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = 0
            _end = embed_dim
            _w = in_proj_weight[_start:_end, :]
            if _b is not None:
                _b = _b[_start:_end]
            q = F.linear(query, _w, _b)

            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = embed_dim
            _end = embed_dim * 2
            _w = in_proj_weight[_start:_end, :]
            if _b is not None:
                _b = _b[_start:_end]
            k = F.linear(key, _w, _b)

            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = embed_dim * 2
            _end = None
            _w = in_proj_weight[_start:, :]
            if _b is not None:
                _b = _b[_start:]
            v = F.linear(value, _w, _b)
    else:
        q_proj_weight_non_opt = torch.jit._unwrap_optional(q_proj_weight)
        len1, len2 = q_proj_weight_non_opt.size()
        assert len1 == embed_dim and len2 == query.size(-1)

        k_proj_weight_non_opt = torch.jit._unwrap_optional(k_proj_weight)
        len1, len2 = k_proj_weight_non_opt.size()
        assert len1 == embed_dim and len2 == key.size(-1)

        v_proj_weight_non_opt = torch.jit._unwrap_optional(v_proj_weight)
        len1, len2 = v_proj_weight_non_opt.size()
        assert len1 == embed_dim and len2 == value.size(-1)

        if in_proj_bias is not None:
            q = F.linear(query, q_proj_weight_non_opt, in_proj_bias[0:embed_dim])
            k = F.linear(key, k_proj_weight_non_opt, in_proj_bias[embed_dim : (embed_dim * 2)])
            v = F.linear(value, v_proj_weight_non_opt, in_proj_bias[(embed_dim * 2) :])
        else:
            q = F.linear(query, q_proj_weight_non_opt, in_proj_bias)
            k = F.linear(key, k_proj_weight_non_opt, in_proj_bias)
            v = F.linear(value, v_proj_weight_non_opt, in_proj_bias)
    q = q * scaling

    if attn_mask is not None:
        assert (
            attn_mask.dtype == torch.float32
            or attn_mask.dtype == torch.float64
            or attn_mask.dtype == torch.float16
            or attn_mask.dtype == torch.uint8
            or attn_mask.dtype == torch.bool
        ), "Only float, byte, and bool types are supported for attn_mask, not {}".format(attn_mask.dtype)
        if attn_mask.dtype == torch.uint8:
            warnings.warn("Byte tensor for attn_mask in nn.MultiheadAttention is deprecated. Use bool tensor instead.")
            attn_mask = attn_mask.to(torch.bool)

        if attn_mask.dim() == 2:
            attn_mask = attn_mask.unsqueeze(0)
            if list(attn_mask.size()) != [1, query.size(0), key.size(0)]:
                raise RuntimeError("The size of the 2D attn_mask is not correct.")
        elif attn_mask.dim() == 3:
            if list(attn_mask.size()) != [bsz * num_heads, query.size(0), key.size(0)]:
                raise RuntimeError("The size of the 3D attn_mask is not correct.")
        else:
            raise RuntimeError("attn_mask's dimension {} is not supported".format(attn_mask.dim()))
        # attn_mask's dim is 3 now.

    # convert ByteTensor key_padding_mask to bool
    if key_padding_mask is not None and key_padding_mask.dtype == torch.uint8:
        warnings.warn("Byte tensor for key_padding_mask in nn.MultiheadAttention is deprecated. Use bool tensor instead.")
        key_padding_mask = key_padding_mask.to(torch.bool)

    if bias_k is not None and bias_v is not None:
        if static_k is None and static_v is None:
            k = torch.cat([k, bias_k.repeat(1, bsz, 1)])
            v = torch.cat([v, bias_v.repeat(1, bsz, 1)])
            if attn_mask is not None:
                attn_mask = F.pad(attn_mask, (0, 1))
            if key_padding_mask is not None:
                key_padding_mask = F.pad(key_padding_mask, (0, 1))
        else:
            assert static_k is None, "bias cannot be added to static key."
            assert static_v is None, "bias cannot be added to static value."
    else:
        assert bias_k is None
        assert bias_v is None

    q = q.contiguous().view(tgt_len, bsz * num_heads, head_dim).transpose(0, 1)
    if k is not None:
        k = k.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)
    if v is not None:
        v = v.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)

    if static_k is not None:
        assert static_k.size(0) == bsz * num_heads
        assert static_k.size(2) == head_dim
        k = static_k

    if static_v is not None:
        assert static_v.size(0) == bsz * num_heads
        assert static_v.size(2) == head_dim
        v = static_v

    src_len = k.size(1)

    if key_padding_mask is not None:
        assert key_padding_mask.size(0) == bsz
        assert key_padding_mask.size(1) == src_len

    if add_zero_attn:
        src_len += 1
        k = torch.cat([k, torch.zeros((k.size(0), 1) + k.size()[2:], dtype=k.dtype, device=k.device)], dim=1)
        v = torch.cat([v, torch.zeros((v.size(0), 1) + v.size()[2:], dtype=v.dtype, device=v.device)], dim=1)
        if attn_mask is not None:
            attn_mask = F.pad(attn_mask, (0, 1))
        if key_padding_mask is not None:
            key_padding_mask = F.pad(key_padding_mask, (0, 1))

    attn_output_weights = torch.bmm(q, k.transpose(1, 2))
    assert list(attn_output_weights.size()) == [bsz * num_heads, tgt_len, src_len]

    if attn_mask is not None:
        if attn_mask.dtype == torch.bool:
            attn_output_weights.masked_fill_(attn_mask, float("-inf"))
        else:
            attn_output_weights += attn_mask

    if key_padding_mask is not None:
        attn_output_weights = attn_output_weights.view(bsz, num_heads, tgt_len, src_len)
        attn_output_weights = attn_output_weights.masked_fill(
            key_padding_mask.unsqueeze(1).unsqueeze(2),
            float("-inf"),
        )
        attn_output_weights = attn_output_weights.view(bsz * num_heads, tgt_len, src_len)

    attn_output_weights_raw = attn_output_weights
    attn_output_weights = F.softmax(attn_output_weights, dim=-1)
    attn_output_weights = F.dropout(attn_output_weights, p=dropout_p, training=training)

    attn_output = torch.bmm(attn_output_weights, v)
    assert list(attn_output.size()) == [bsz * num_heads, tgt_len, head_dim]
    attn_output = attn_output.transpose(0, 1).contiguous().view(tgt_len, bsz, embed_dim)
    attn_output = F.linear(attn_output, out_proj_weight, out_proj_bias)

    if need_weights:
        if need_raw:
            attn_output_weights_raw = attn_output_weights_raw.view(bsz, num_heads, tgt_len, src_len)
            return attn_output, attn_output_weights_raw

        else:
            # average attention weights over heads
            attn_output_weights = attn_output_weights.view(bsz, num_heads, tgt_len, src_len)
            return attn_output, attn_output_weights.sum(dim=1) / num_heads
    else:
        return attn_output, None


class MultiheadAttention(Module):
    r"""Allows the model to jointly attend to information
    from different representation subspaces.
    See reference: Attention Is All You Need

    .. math::
        \text{MultiHead}(Q, K, V) = \text{Concat}(head_1,\dots,head_h)W^O
        \text{where} head_i = \text{Attention}(QW_i^Q, KW_i^K, VW_i^V)

    Args:
        embed_dim: total dimension of the model.
        num_heads: parallel attention heads.
        dropout: a Dropout layer on attn_output_weights. Default: 0.0.
        bias: add bias as module parameter. Default: True.
        add_bias_kv: add bias to the key and value sequences at dim=0.
        add_zero_attn: add a new batch of zeros to the key and
                       value sequences at dim=1.
        kdim: total number of features in key. Default: None.
        vdim: total number of features in value. Default: None.

        Note: if kdim and vdim are None, they will be set to embed_dim such that
        query, key, and value have the same number of features.

    Examples::

        >>> multihead_attn = nn.MultiheadAttention(embed_dim, num_heads)
        >>> attn_output, attn_output_weights = multihead_attn(query, key, value)
    """
    bias_k: Optional[torch.Tensor]
    bias_v: Optional[torch.Tensor]

    def __init__(
        self, embed_dim, num_heads, dropout=0.0, bias=True, add_bias_kv=False, add_zero_attn=False, kdim=None, vdim=None
    ):
        super(MultiheadAttention, self).__init__()
        self.embed_dim = embed_dim
        self.kdim = kdim if kdim is not None else embed_dim
        self.vdim = vdim if vdim is not None else embed_dim
        self._qkv_same_embed_dim = self.kdim == embed_dim and self.vdim == embed_dim

        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"

        if self._qkv_same_embed_dim is False:
            self.q_proj_weight = Parameter(torch.Tensor(embed_dim, embed_dim))
            self.k_proj_weight = Parameter(torch.Tensor(embed_dim, self.kdim))
            self.v_proj_weight = Parameter(torch.Tensor(embed_dim, self.vdim))
            self.register_parameter("in_proj_weight", None)
        else:
            self.in_proj_weight = Parameter(torch.empty(3 * embed_dim, embed_dim))
            self.register_parameter("q_proj_weight", None)
            self.register_parameter("k_proj_weight", None)
            self.register_parameter("v_proj_weight", None)

        if bias:
            self.in_proj_bias = Parameter(torch.empty(3 * embed_dim))
        else:
            self.register_parameter("in_proj_bias", None)
        self.out_proj = _LinearWithBias(embed_dim, embed_dim)

        if add_bias_kv:
            self.bias_k = Parameter(torch.empty(1, 1, embed_dim))
            self.bias_v = Parameter(torch.empty(1, 1, embed_dim))
        else:
            self.bias_k = self.bias_v = None

        self.add_zero_attn = add_zero_attn

        self._reset_parameters()

    def _reset_parameters(self):
        if self._qkv_same_embed_dim:
            xavier_uniform_(self.in_proj_weight)
        else:
            xavier_uniform_(self.q_proj_weight)
            xavier_uniform_(self.k_proj_weight)
            xavier_uniform_(self.v_proj_weight)

        if self.in_proj_bias is not None:
            constant_(self.in_proj_bias, 0.0)
            constant_(self.out_proj.bias, 0.0)
        if self.bias_k is not None:
            xavier_normal_(self.bias_k)
        if self.bias_v is not None:
            xavier_normal_(self.bias_v)

    def __setstate__(self, state):
        # Support loading old MultiheadAttention checkpoints generated by v1.1.0
        if "_qkv_same_embed_dim" not in state:
            state["_qkv_same_embed_dim"] = True

        super(MultiheadAttention, self).__setstate__(state)

    def forward(self, query, key, value, key_padding_mask=None, need_weights=True, need_raw=True, attn_mask=None):
        r"""
        Args:
            query, key, value: map a query and a set of key-value pairs to an output.
                See "Attention Is All You Need" for more details.
            key_padding_mask: if provided, specified padding elements in the key will
                be ignored by the attention. When given a binary mask and a value is True,
                the corresponding value on the attention layer will be ignored. When given
                a byte mask and a value is non-zero, the corresponding value on the attention
                layer will be ignored
            need_weights: output attn_output_weights.
            attn_mask: 2D or 3D mask that prevents attention to certain positions. A 2D mask will be broadcasted for all
                the batches while a 3D mask allows to specify a different mask for the entries of each batch.

        Shape:
            - Inputs:
            - query: :math:`(L, N, E)` where L is the target sequence length, N is the batch size, E is
              the embedding dimension.
            - key: :math:`(S, N, E)`, where S is the source sequence length, N is the batch size, E is
              the embedding dimension.
            - value: :math:`(S, N, E)` where S is the source sequence length, N is the batch size, E is
              the embedding dimension.
            - key_padding_mask: :math:`(N, S)` where N is the batch size, S is the source sequence length.
              If a ByteTensor is provided, the non-zero positions will be ignored while the position
              with the zero positions will be unchanged. If a BoolTensor is provided, the positions with the
              value of ``True`` will be ignored while the position with the value of ``False`` will be unchanged.
            - attn_mask: 2D mask :math:`(L, S)` where L is the target sequence length, S is the source sequence length.
              3D mask :math:`(N*num_heads, L, S)` where N is the batch size, L is the target sequence length,
              S is the source sequence length. attn_mask ensure that position i is allowed to attend the unmasked
              positions. If a ByteTensor is provided, the non-zero positions are not allowed to attend
              while the zero positions will be unchanged. If a BoolTensor is provided, positions with ``True``
              is not allowed to attend while ``False`` values will be unchanged. If a FloatTensor
              is provided, it will be added to the attention weight.

            - Outputs:
            - attn_output: :math:`(L, N, E)` where L is the target sequence length, N is the batch size,
              E is the embedding dimension.
            - attn_output_weights: :math:`(N, L, S)` where N is the batch size,
              L is the target sequence length, S is the source sequence length.
        """
        if not self._qkv_same_embed_dim:
            return multi_head_attention_forward(
                query,
                key,
                value,
                self.embed_dim,
                self.num_heads,
                self.in_proj_weight,
                self.in_proj_bias,
                self.bias_k,
                self.bias_v,
                self.add_zero_attn,
                self.dropout,
                self.out_proj.weight,
                self.out_proj.bias,
                training=self.training,
                key_padding_mask=key_padding_mask,
                need_weights=need_weights,
                need_raw=need_raw,
                attn_mask=attn_mask,
                use_separate_proj_weight=True,
                q_proj_weight=self.q_proj_weight,
                k_proj_weight=self.k_proj_weight,
                v_proj_weight=self.v_proj_weight,
            )
        else:
            return multi_head_attention_forward(
                query,
                key,
                value,
                self.embed_dim,
                self.num_heads,
                self.in_proj_weight,
                self.in_proj_bias,
                self.bias_k,
                self.bias_v,
                self.add_zero_attn,
                self.dropout,
                self.out_proj.weight,
                self.out_proj.bias,
                training=self.training,
                key_padding_mask=key_padding_mask,
                need_weights=need_weights,
                need_raw=need_raw,
                attn_mask=attn_mask,
            )
class Transformer(nn.Module):
    def __init__(self, feature_dim=512):
        super(Transformer, self).__init__()
        # Encoder
        self.cls_token = nn.Parameter(torch.randn(1, 1, feature_dim))
        nn.init.normal_(self.cls_token, std=1e-6)
        # self.layer1 = TransLayer(dim=feature_dim)
        self.layer2 = TransLayer(dim=feature_dim)
        self.norm = nn.LayerNorm(feature_dim)
        # Decoder

    def forward(self, features):
        # ---->token
        cls_tokens = self.cls_token.expand(features.shape[0], -1, -1)
        h = torch.cat((cls_tokens, features), dim=1)
        # ---->Translayer x1
        # h = self.layer1(h)  # [B, N, 512]
        # ---->Translayer x2
        h = self.layer2(h)  # [B, N, 512]
        # ---->cls_token
        h = self.norm(h)
        return h[:, 0], h[:, 1:]

# helper functions
def exists(val):
    return val is not None

def moore_penrose_iter_pinv(x, iters=6):
    device = x.device

    abs_x = torch.abs(x)
    col = abs_x.sum(dim=-1)
    row = abs_x.sum(dim=-2)
    z = rearrange(x, "... i j -> ... j i") / (torch.max(col) * torch.max(row))

    I = torch.eye(x.shape[-1], device=device)
    I = rearrange(I, "i j -> () i j")

    for _ in range(iters):
        xz = x @ z
        z = 0.25 * z @ (13 * I - (xz @ (15 * I - (xz @ (7 * I - xz)))))

    return z
class NystromAttention(nn.Module):
    def __init__(
        self,
        dim,
        dim_head=64,
        heads=8,
        num_landmarks=256,
        pinv_iterations=6,
        residual=True,
        residual_conv_kernel=33,
        eps=1e-8,
        dropout=0.0,
    ):
        super().__init__()
        self.eps = eps
        inner_dim = heads * dim_head

        self.num_landmarks = num_landmarks
        self.pinv_iterations = pinv_iterations

        self.heads = heads
        self.scale = dim_head**-0.5
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias=False)

        self.to_out = nn.Sequential(nn.Linear(inner_dim, dim), nn.Dropout(dropout))

        self.residual = residual
        if residual:
            kernel_size = residual_conv_kernel
            padding = residual_conv_kernel // 2
            self.res_conv = nn.Conv2d(heads, heads, (kernel_size, 1), padding=(padding, 0), groups=heads, bias=False)

    def forward(self, x, mask=None, return_attn=False):
        b, n, _, h, m, iters, eps = *x.shape, self.heads, self.num_landmarks, self.pinv_iterations, self.eps

        # pad so that sequence can be evenly divided into m landmarks

        remainder = n % m
        if remainder > 0:
            padding = m - (n % m)
            x = F.pad(x, (0, 0, padding, 0), value=0)

            if exists(mask):
                mask = F.pad(mask, (padding, 0), value=False)

        # derive query, keys, values

        q, k, v = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, "b n (h d) -> b h n d", h=h), (q, k, v))

        # set masked positions to 0 in queries, keys, values

        if exists(mask):
            mask = rearrange(mask, "b n -> b () n")
            q, k, v = map(lambda t: t * mask[..., None], (q, k, v))

        q = q * self.scale

        # generate landmarks by sum reduction, and then calculate mean using the mask

        l = ceil(n / m)
        landmark_einops_eq = "... (n l) d -> ... n d"
        q_landmarks = reduce(q, landmark_einops_eq, "sum", l=l)
        k_landmarks = reduce(k, landmark_einops_eq, "sum", l=l)

        # calculate landmark mask, and also get sum of non-masked elements in preparation for masked mean

        divisor = l
        if exists(mask):
            mask_landmarks_sum = reduce(mask, "... (n l) -> ... n", "sum", l=l)
            divisor = mask_landmarks_sum[..., None] + eps
            mask_landmarks = mask_landmarks_sum > 0

        # masked mean (if mask exists)

        q_landmarks /= divisor
        k_landmarks /= divisor

        # similarities

        einops_eq = "... i d, ... j d -> ... i j"
        sim1 = einsum(einops_eq, q, k_landmarks)
        sim2 = einsum(einops_eq, q_landmarks, k_landmarks)
        sim3 = einsum(einops_eq, q_landmarks, k)

        # masking

        if exists(mask):
            mask_value = -torch.finfo(q.dtype).max
            sim1.masked_fill_(~(mask[..., None] * mask_landmarks[..., None, :]), mask_value)
            sim2.masked_fill_(~(mask_landmarks[..., None] * mask_landmarks[..., None, :]), mask_value)
            sim3.masked_fill_(~(mask_landmarks[..., None] * mask[..., None, :]), mask_value)

        # eq (15) in the paper and aggregate values

        attn1, attn2, attn3 = map(lambda t: t.softmax(dim=-1), (sim1, sim2, sim3))
        attn2_inv = moore_penrose_iter_pinv(attn2, iters)

        out = (attn1 @ attn2_inv) @ (attn3 @ v)

        # add depth-wise conv residual of values

        if self.residual:
            out += self.res_conv(v)

        # merge and combine heads

        out = rearrange(out, "b h n d -> b n (h d)", h=h)
        out = self.to_out(out)
        out = out[:, -n:]

        if return_attn:
            attn = attn1 @ attn2_inv @ attn3
            return out, attn

        return out
class TransLayer(nn.Module):
    def __init__(self, norm_layer=nn.LayerNorm, dim=512):
        super().__init__()
        self.norm = norm_layer(dim)
        self.attn = NystromAttention(
            dim=dim,
            dim_head=dim // 4, # dim//8
            heads=4,# 8
            num_landmarks=dim // 4,  # number of landmarks
            pinv_iterations=6,  # number of moore-penrose iterations for approximating pinverse. 6 was recommended by the paper
            residual=True,  # whether to do an extra residual with the value or not. supposedly faster convergence if turned on
            dropout=0.25,
        )

    def forward(self, x):
        x = x + self.attn(self.norm(x))
        return x
def SNN_Block(dim1, dim2, dropout=0.25):
    r"""
    Multilayer Reception Block w/ Self-Normalization (Linear + ELU + Alpha Dropout)

    args:
        dim1 (int): Dimension of input features
        dim2 (int): Dimension of output features
        dropout (float): Dropout rate
    """
    return nn.Sequential(nn.Linear(dim1, dim2), nn.ELU(), nn.AlphaDropout(p=dropout, inplace=False))
class MLP(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x

class ConvNet(nn.Module):
    def __init__(self):
        super(ConvNet,self).__init__()
        self.conv1 = nn.Sequential(
            nn.Conv1d(1, 16, kernel_size=20, padding=0),
            nn.BatchNorm1d(16),
            nn.ReLU()
        )
        # self.conv2 = nn.Sequential(
        #     nn.Conv1d(16, 32, kernel_size=19, padding=0),
        #     nn.BatchNorm1d(32),
        #     nn.ReLU()
        # )
        self.maxpool = nn.MaxPool1d(2)
        self.conv3 = nn.Sequential(
            nn.Conv1d(16, 64, kernel_size=10, padding=0),
            nn.BatchNorm1d(64),
            nn.ReLU()
        )
        self.fc = nn.Sequential(
            nn.Linear(80192, 256), #7680,10880
            nn.ReLU(),
            nn.Linear(256, 1)
            # nn.ReLU()
        )

    def forward(self, out):
        # print("input ", out.shape)
        out = self.conv1(out)
        # print("out1 ",out.shape)
        out = self.maxpool(out)
        # out = self.conv2(out)
        # print("out2", out.shape)
        out = self.conv3(out)
        out = self.maxpool(out)
        # print("out3",out.shape)
        out = out.view(out.size(0), -1)
        # print("before fc",out.shape)
        out = self.fc(out)
        return out

    def __hasattr__(self, name):
        if '_parameters' in self.__dict__:
            _parameters = self.__dict__['_parameters']
            if name in _parameters:
                return True
        if '_buffers' in self.__dict__:
            _buffers = self.__dict__['_buffers']
            if name in _buffers:
                return True
        if '_modules' in self.__dict__:
            modules = self.__dict__['_modules']
            if name in modules:
                return True
        return False


class TrCross(nn.Module):
    def __init__(self, args, model_size="small",omic_sizes=[58, 290, 290, 155]):
        super(TrCross, self).__init__()
        self.size_dict = {
            "Radiology": {"small": [863, 512, 256], "large": [1024, 512, 256]},
            "pathomics": {"small": [512, 256], "large": [1024, 1024, 1024, 256]},
        }
        self.dim = args.feature_dim

        # Radiology Embedding Network
        hidden = self.size_dict["Radiology"][model_size]
        fc = []
        for idx in range(len(hidden) - 1):
            fc.append(nn.Linear(hidden[idx], hidden[idx + 1]))
            fc.append(nn.ReLU())
            # fc.append(nn.Dropout(0.0))#0.25
        self.Radiology_fc = nn.Sequential(*fc)

        # Pathomics Embedding Network
        hidden = self.size_dict["pathomics"][model_size]
        sig_networks = []
        for input_dim in omic_sizes:
            fc_omic = [SNN_Block(dim1=input_dim, dim2=hidden[0])]
            for i, _ in enumerate(hidden[1:]):
                fc_omic.append(SNN_Block(dim1=hidden[i], dim2=hidden[i + 1], dropout=0.0))#0.25
            sig_networks.append(nn.Sequential(*fc_omic))
        self.Pathomics_fc = nn.ModuleList(sig_networks)
        ###trsformer
        # Encoder
        self.radiology_encoder = Transformer(self.dim)
        # Decoder
        self.radiology_decoder = Transformer(self.dim)


        ###crossAttention
        self.R_In_P= MultiheadAttention(embed_dim=args.feature_dim, num_heads=1)
        self.P_In_R = MultiheadAttention(embed_dim=args.feature_dim, num_heads=1)

        # Encoder
        self.pathomics_encoder = Transformer(self.dim)
        # Decoder
        self.pathomics_decoder = Transformer(self.dim)
        ####MLP

        # self.bbox_embed = MLP(self.dim*2, self.dim*2, 1, 1)
        self.bbox_embed = MLP(self.dim, self.dim, 1, 1)

        ######MLIF
        self.fusion = define_bifusion(fusion_type=args.fusion_type, skip=args.skip, use_bilinear=args.use_bilinear,
                                      gate1=args.path_gate, gate2=args.omic_gate, dim1=args.path_dim, dim2=args.omic_dim,
                                      scale_dim1=args.path_scale, scale_dim2=args.omic_scale, mmhid=args.mmhid,
                                      dropout_rate=args.dropout_rate)
        self.classifier = MLP(self.dim, self.dim, 1, 1)
        self.classifier2 = MLP(self.dim*2, self.dim*2, 1, 1)
        act = define_act_layer(act_type=args.act_type)


        self.act = act
        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

    def forward(self, **kwargs):

        x_ra = kwargs["ra"]
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]

        #ra embedding
        radiology_features = self.Radiology_fc(x_ra)

        #pa embedding
        pathomics_features = [self.Pathomics_fc[idx].forward(sig_feat) for idx, sig_feat in enumerate(x_pa)]
        pathomics_features = torch.stack(pathomics_features)
        pathomics_features = pathomics_features.transpose(1,0)

        # ra encoder
        cls_token_ra_encoder, patch_token_ra_encoder = self.radiology_encoder(
            radiology_features)  # cls token + patch tokens
        # pa encoder
        cls_token_pa_encoder, patch_token_pa_encoder = self.pathomics_encoder(
            pathomics_features)  # cls token + patch tokens

        # cross-omics attention
        ra_in_pa, Att = self.R_In_P(
            patch_token_ra_encoder.transpose(1, 0),
            patch_token_pa_encoder.transpose(1, 0),
            patch_token_pa_encoder.transpose(1, 0),
        )  # ([5, 16, 256])
        pa_in_ra, Att = self.P_In_R(
            patch_token_pa_encoder.transpose(1, 0),
            patch_token_ra_encoder.transpose(1, 0),
            patch_token_ra_encoder.transpose(1, 0),
        )  # ([4, 16, 256])

        # decoder
        # radiology decoder
        cls_token_radiology_decoder, _ = self.radiology_decoder(
            ra_in_pa.transpose(1, 0))  # cls token + patch tokens
        # genomics decoder
        cls_token_pathomics_decoder, _ = self.pathomics_decoder(
            pa_in_ra.transpose(1, 0))  # cls token + patch tokens

        features = self.fusion(cls_token_radiology_decoder, cls_token_pathomics_decoder)
        features2 = self.fusion(cls_token_ra_encoder, cls_token_pa_encoder)
        out = torch.cat((features, features2), 1)
        hazard = self.classifier2(out)
        # hazard = self.classifier(features)
        if self.act is not None:
            hazard = self.act(hazard)

            if isinstance(self.act, nn.Sigmoid):
                hazard = hazard * self.output_range + self.output_shift

        return features, hazard, cls_token_ra_encoder,cls_token_radiology_decoder,cls_token_pa_encoder,cls_token_pathomics_decoder


class PathomicNet(nn.Module):
    def __init__(self, args, act, k):
        super(PathomicNet, self).__init__()

        self.fusion = define_bifusion(fusion_type=args.fusion_type, skip=args.skip, use_bilinear=args.use_bilinear, gate1=args.path_gate, gate2=args.omic_gate, dim1=args.path_dim, dim2=args.omic_dim, scale_dim1=args.path_scale, scale_dim2=args.omic_scale, mmhid=args.mmhid, dropout_rate=args.dropout_rate)
        self.classifier = nn.Sequential(nn.Linear(args.mmhid, args.label_dim))
        self.act = act


        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

        # self.Concat = Concat(skip=opt.skip, use_bilinear=opt.use_bilinear, gate1=opt.path_gate, gate2=opt.omic_gate, dim1=opt.path_dim, dim2=opt.omic_dim, scale_dim1=opt.path_scale, scale_dim2=opt.omic_scale, mmhid=opt.mmhid, dropout_rate=opt.dropout_rate)


    def forward(self, **kwargs):

        path_vec = kwargs["ra"]
        omic_vec = [kwargs["pa%d" % i] for i in range(1, 5)]

        # ra embedding
        radiology_features = self.Radiology_fc(path_vec)

        # pa embedding
        pathomics_features = [self.Pathomics_fc[idx].forward(sig_feat) for idx, sig_feat in enumerate(omic_vec)]
        pathomics_features = torch.stack(pathomics_features)
        pathomics_features = pathomics_features.transpose(1, 0)

        # ra encoder
        cls_token_ra_encoder, patch_token_ra_encoder = self.radiology_encoder(
            radiology_features)  # cls token + patch tokens
        # pa encoder
        cls_token_pa_encoder, patch_token_pa_encoder = self.pathomics_encoder(
            pathomics_features)  # cls token + patch tokens

        # cross-omics attention
        ra_in_pa, Att = self.R_In_P(
            patch_token_ra_encoder.transpose(1, 0),
            patch_token_pa_encoder.transpose(1, 0),
            patch_token_pa_encoder.transpose(1, 0),
        )  # ([5, 16, 256])
        pa_in_ra, Att = self.P_In_R(
            patch_token_pa_encoder.transpose(1, 0),
            patch_token_ra_encoder.transpose(1, 0),
            patch_token_ra_encoder.transpose(1, 0),
        )  # ([4, 16, 256])

        # decoder
        # radiology decoder
        cls_token_radiology_decoder, _ = self.radiology_decoder(
            ra_in_pa.transpose(1, 0))  # cls token + patch tokens
        # genomics decoder
        cls_token_pathomics_decoder, _ = self.pathomics_decoder(
            pa_in_ra.transpose(1, 0))  # cls token + patch tokens

        features = self.fusion(cls_token_radiology_decoder, cls_token_pathomics_decoder)
        features2 = self.fusion(cls_token_ra_encoder, cls_token_pa_encoder)
        out = torch.cat((features, features2, path_vec,omic_vec), 1)
        hazard = self.classifier2(out)

        if self.act is not None:
            hazard = self.act(hazard)

            if isinstance(self.act, nn.Sigmoid):
                hazard = hazard * self.output_range + self.output_shift

        return features, hazard, cls_token_ra_encoder, cls_token_radiology_decoder, cls_token_pa_encoder, cls_token_pathomics_decoder

        if self.act is not None:
            hazard = self.act(hazard)

            if isinstance(self.act, nn.Sigmoid):
                hazard = hazard * self.output_range + self.output_shift

        return features, hazard

    def __hasattr__(self, name):
        if '_parameters' in self.__dict__:
            _parameters = self.__dict__['_parameters']
            if name in _parameters:
                return True
        if '_buffers' in self.__dict__:
            _buffers = self.__dict__['_buffers']
            if name in _buffers:
                return True
        if '_modules' in self.__dict__:
            modules = self.__dict__['_modules']
            if name in modules:
                return True
        return False

class RANet(nn.Module):
    def __init__(self, args, model_size="small",omic_sizes=[58, 290, 290, 155]):
        super(RANet, self).__init__()
        self.size_dict = {
            "Radiology": {"small": [863, 512, 256], "large": [1024, 512, 256]},
            "pathomics": {"small": [512, 256], "large": [1024, 1024, 1024, 256]},
        }
        self.dim = args.feature_dim

        # Radiology Embedding Network
        hidden = self.size_dict["Radiology"][model_size]
        fc = []
        for idx in range(len(hidden) - 1):
            fc.append(nn.Linear(hidden[idx], hidden[idx + 1]))
            fc.append(nn.ReLU())
            # fc.append(nn.Dropout(0.0))#0.25
        self.Radiology_fc = nn.Sequential(*fc)

        # Pathomics Embedding Network
        hidden = self.size_dict["pathomics"][model_size]
        sig_networks = []
        for input_dim in omic_sizes:
            fc_omic = [SNN_Block(dim1=input_dim, dim2=hidden[0])]
            for i, _ in enumerate(hidden[1:]):
                fc_omic.append(SNN_Block(dim1=hidden[i], dim2=hidden[i + 1], dropout=0.0))#0.25
            sig_networks.append(nn.Sequential(*fc_omic))
        self.Pathomics_fc = nn.ModuleList(sig_networks)
        ###trsformer
        # Encoder
        self.radiology_encoder = Transformer(self.dim)
        # Decoder
        self.radiology_decoder = Transformer(self.dim)

        # Encoder
        self.pathomics_encoder = Transformer(self.dim)
        # Decoder
        self.pathomics_decoder = Transformer(self.dim)
        ####MLP

        self.bbox_embed = MLP(self.dim, self.dim, 1, 1)
    def forward(self, **kwargs):

        x_ra = kwargs["ra"]
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]

        #ra embedding
        radiology_features = self.Radiology_fc(x_ra)

        # ra encoder
        cls_token_ra_encoder, patch_token_ra_encoder = self.radiology_encoder(
            radiology_features)  # cls token + patch tokens

        # decoder
        # radiology decoder
        cls_token_radiology_decoder, _ = self.radiology_decoder(
            radiology_features.transpose(1, 0))  # cls token + patch tokens

        hazard = self.bbox_embed(cls_token_ra_encoder)
        # features = []

        return radiology_features, hazard

class PATHNet(nn.Module):
    def __init__(self, args, model_size="small",omic_sizes=[58, 290, 290, 155]):
        super(PATHNet, self).__init__()
        self.size_dict = {
            "Radiology": {"small": [863, 512, 256], "large": [1024, 512, 256]},
            "pathomics": {"small": [512, 256], "large": [1024, 1024, 1024, 256]},
        }
        self.dim = args.feature_dim

        # Radiology Embedding Network
        hidden = self.size_dict["Radiology"][model_size]
        fc = []
        for idx in range(len(hidden) - 1):
            fc.append(nn.Linear(hidden[idx], hidden[idx + 1]))
            fc.append(nn.ReLU())
            # fc.append(nn.Dropout(0.0))#0.25
        self.Radiology_fc = nn.Sequential(*fc)

        # Pathomics Embedding Network
        hidden = self.size_dict["pathomics"][model_size]
        sig_networks = []
        for input_dim in omic_sizes:
            fc_omic = [SNN_Block(dim1=input_dim, dim2=hidden[0])]
            for i, _ in enumerate(hidden[1:]):
                fc_omic.append(SNN_Block(dim1=hidden[i], dim2=hidden[i + 1], dropout=0.0))#0.25
            sig_networks.append(nn.Sequential(*fc_omic))
        self.Pathomics_fc = nn.ModuleList(sig_networks)
        ###trsformer
        # Encoder
        self.radiology_encoder = Transformer(self.dim)
        # Decoder
        self.radiology_decoder = Transformer(self.dim)

        # Encoder
        self.pathomics_encoder = Transformer(self.dim)
        # Decoder
        self.pathomics_decoder = Transformer(self.dim)
        ####MLP

        self.bbox_embed = MLP(self.dim, self.dim, 1, 1)
    def forward(self, **kwargs):

        x_ra = kwargs["ra"]
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]

        # #ra embedding
        # radiology_features = self.Radiology_fc(x_ra)

        #pa embedding
        pathomics_features = [self.Pathomics_fc[idx].forward(sig_feat) for idx, sig_feat in enumerate(x_pa)]
        pathomics_features = torch.stack(pathomics_features)
        pathomics_features = pathomics_features.transpose(1,0)

        # ra encoder
        # cls_token_ra_encoder, patch_token_ra_encoder = self.radiology_encoder(
        #     radiology_features)  # cls token + patch tokens
        # pa encoder
        cls_token_pa_encoder, patch_token_pa_encoder = self.pathomics_encoder(
            pathomics_features)  # cls token + patch tokens

        # decoder
        # radiology decoder
        # cls_token_radiology_decoder, _ = self.radiology_decoder(
        #     pathomics_features.transpose(1, 0))  # cls token + patch tokens
        # # genomics decoder
        cls_token_pathomics_decoder, _ = self.pathomics_decoder(
            pathomics_features.transpose(1, 0))  # cls token + patch tokens

        hazard = self.bbox_embed(cls_token_pa_encoder)
        # features = []c

        return pathomics_features, hazard


class PATHNet2(nn.Module):
    def __init__(self, args, input_dim=793, path_dim=32, dropout_rate=0.25, act=None, label_dim=1, init_max=True):
        super(PATHNet2, self).__init__()
        hidden = [256, 128, 64, 32]
        self.act = act

        encoder1 = nn.Sequential(
            nn.Linear(input_dim, hidden[0]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder2 = nn.Sequential(
            nn.Linear(hidden[0], hidden[1]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder3 = nn.Sequential(
            nn.Linear(hidden[1], hidden[2]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder4 = nn.Sequential(
            nn.Linear(hidden[2], path_dim),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        # encoder5 = nn.Sequential(
        #     nn.Linear(omic_dim, 32),
        #     nn.SiLU(),
        #     nn.AlphaDropout(p=dropout_rate, inplace=False))

        self.encoder = nn.Sequential(encoder1, encoder2, encoder3,encoder4)
        self.classifier = nn.Sequential(nn.Linear(path_dim, label_dim))

        # if init_max: init_max_weights(self)

        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

    def forward(self, **kwargs):
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]
        # pa embedding
        # path_features = [self.Pathomics_fc[idx].forward(sig_feat) for idx, sig_feat in enumerate(x_pa)]
        path_PaTu_features = x_pa[0]
        path_PaEp_features = x_pa[1]
        path_PaSt_features = x_pa[2]
        path_PaNu_features = x_pa[3]
        path_features = torch.concat((path_PaTu_features, path_PaEp_features,path_PaSt_features,path_PaNu_features), dim=1)
        # x = x.to(torch.float32)
        features = self.encoder(path_features)
        out = self.classifier(features)
        if self.act is not None:
            out = self.act(out)

            if isinstance(self.act, nn.Sigmoid):
                out = out * self.output_range + self.output_shift

        return features, out

class PATHNet_TU(nn.Module):
    def __init__(self, args, input_dim=58, path_dim=32, dropout_rate=0.25, act=None, label_dim=1, init_max=True):
        super(PATHNet_TU, self).__init__()
        self.act = act

        self.encoder = nn.Sequential(
            nn.Linear(input_dim, path_dim),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))
        self.classifier = nn.Sequential(nn.Linear(path_dim, label_dim))

        # if init_max: init_max_weights(self)

        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

    def forward(self, **kwargs):
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]
        path_PaTu_features = x_pa[0]
        # path_PaEp_features = x_pa[1]
        # path_PaSt_features = x_pa[2]
        # path_PaNu_features = x_pa[3]
        # x = x.to(torch.float32)
        features = self.encoder(path_PaTu_features)
        out = self.classifier(features)
        if self.act is not None:
            out = self.act(out)

            if isinstance(self.act, nn.Sigmoid):
                out = out * self.output_range + self.output_shift

        return features, out

class PATHNet_PaEp(nn.Module):
    def __init__(self, args, input_dim=290, path_dim=32, dropout_rate=0.25, act=None, label_dim=1, init_max=True):
        super(PATHNet_PaEp, self).__init__()
        hidden = [256, 128, 64, 32]
        self.act = act

        encoder1 = nn.Sequential(
            nn.Linear(input_dim, hidden[0]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder2 = nn.Sequential(
            nn.Linear(hidden[0], hidden[1]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder3 = nn.Sequential(
            nn.Linear(hidden[1], hidden[2]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder4 = nn.Sequential(
            nn.Linear(hidden[2], path_dim),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        # encoder5 = nn.Sequential(
        #     nn.Linear(omic_dim, 32),
        #     nn.SiLU(),
        #     nn.AlphaDropout(p=dropout_rate, inplace=False))

        self.encoder = nn.Sequential(encoder1, encoder2, encoder3, encoder4)
        self.classifier = nn.Sequential(nn.Linear(path_dim, label_dim))

        # if init_max: init_max_weights(self)

        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

    def forward(self, **kwargs):
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]
        path_PaTu_features = x_pa[0]
        path_PaEp_features = x_pa[1]
        path_PaSt_features = x_pa[2]
        path_PaNu_features = x_pa[3]
        # x = x.to(torch.float32)
        features = self.encoder(path_PaEp_features)
        out = self.classifier(features)
        if self.act is not None:
            out = self.act(out)

            if isinstance(self.act, nn.Sigmoid):
                out = out * self.output_range + self.output_shift

        return features, out

class PATHNet_PaSt(nn.Module):
    def __init__(self, args, input_dim=290, path_dim=32, dropout_rate=0.25, act=None, label_dim=1, init_max=True):
        super(PATHNet_PaSt, self).__init__()
        hidden = [256, 128, 64, 32]
        self.act = act

        encoder1 = nn.Sequential(
            nn.Linear(input_dim, hidden[0]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder2 = nn.Sequential(
            nn.Linear(hidden[0], hidden[1]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder3 = nn.Sequential(
            nn.Linear(hidden[1], hidden[2]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder4 = nn.Sequential(
            nn.Linear(hidden[2], path_dim),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        # encoder5 = nn.Sequential(
        #     nn.Linear(omic_dim, 32),
        #     nn.SiLU(),
        #     nn.AlphaDropout(p=dropout_rate, inplace=False))

        self.encoder = nn.Sequential(encoder1, encoder2, encoder3, encoder4)
        self.classifier = nn.Sequential(nn.Linear(path_dim, label_dim))

        # if init_max: init_max_weights(self)

        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

    def forward(self, **kwargs):
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]
        path_PaTu_features = x_pa[0]
        path_PaEp_features = x_pa[1]
        path_PaSt_features = x_pa[2]
        path_PaNu_features = x_pa[3]
        # x = x.to(torch.float32)
        features = self.encoder(path_PaSt_features)
        out = self.classifier(features)
        if self.act is not None:
            out = self.act(out)

            if isinstance(self.act, nn.Sigmoid):
                out = out * self.output_range + self.output_shift

        return features, out

class PATHNet_PaSt(nn.Module):
    def __init__(self, args, input_dim=290, path_dim=32, dropout_rate=0.25, act=None, label_dim=1, init_max=True):
        super(PATHNet_PaSt, self).__init__()
        hidden = [256, 128, 64, 32]
        self.act = act

        encoder1 = nn.Sequential(
            nn.Linear(input_dim, hidden[0]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder2 = nn.Sequential(
            nn.Linear(hidden[0], hidden[1]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder3 = nn.Sequential(
            nn.Linear(hidden[1], hidden[2]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder4 = nn.Sequential(
            nn.Linear(hidden[2], path_dim),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        # encoder5 = nn.Sequential(
        #     nn.Linear(omic_dim, 32),
        #     nn.SiLU(),
        #     nn.AlphaDropout(p=dropout_rate, inplace=False))

        self.encoder = nn.Sequential(encoder1, encoder2, encoder3, encoder4)
        self.classifier = nn.Sequential(nn.Linear(path_dim, label_dim))

        # if init_max: init_max_weights(self)

        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

    def forward(self, **kwargs):
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]
        path_PaTu_features = x_pa[0]
        path_PaEp_features = x_pa[1]
        path_PaSt_features = x_pa[2]
        path_PaNu_features = x_pa[3]
        # x = x.to(torch.float32)
        features = self.encoder(path_PaSt_features)
        out = self.classifier(features)
        if self.act is not None:
            out = self.act(out)

            if isinstance(self.act, nn.Sigmoid):
                out = out * self.output_range + self.output_shift

        return features, out

class PATHNet_PaNu(nn.Module):
    def __init__(self, args, input_dim=155, path_dim=32, dropout_rate=0.25, act=None, label_dim=1, init_max=True):
        super(PATHNet_PaNu, self).__init__()
        hidden = [128, 64, 32]
        self.act = act

        encoder1 = nn.Sequential(
            nn.Linear(input_dim, hidden[0]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder2 = nn.Sequential(
            nn.Linear(hidden[0], hidden[1]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        encoder3 = nn.Sequential(
            nn.Linear(hidden[1], hidden[2]),
            nn.SiLU(),
            nn.AlphaDropout(p=dropout_rate, inplace=False))

        # encoder4 = nn.Sequential(
        #     nn.Linear(hidden[2], path_dim),
        #     nn.SiLU(),
        #     nn.AlphaDropout(p=dropout_rate, inplace=False))

        # encoder5 = nn.Sequential(
        #     nn.Linear(omic_dim, 32),
        #     nn.SiLU(),
        #     nn.AlphaDropout(p=dropout_rate, inplace=False))

        self.encoder = nn.Sequential(encoder1, encoder2, encoder3)
        self.classifier = nn.Sequential(nn.Linear(path_dim, label_dim))

        # if init_max: init_max_weights(self)

        self.output_range = Parameter(torch.FloatTensor([6]), requires_grad=False)
        self.output_shift = Parameter(torch.FloatTensor([-3]), requires_grad=False)

    def forward(self, **kwargs):
        x_pa = [kwargs["pa%d" % i] for i in range(1, 5)]
        path_PaTu_features = x_pa[0]
        path_PaEp_features = x_pa[1]
        path_PaSt_features = x_pa[2]
        path_PaNu_features = x_pa[3]
        # x = x.to(torch.float32)
        features = self.encoder(path_PaNu_features)
        out = self.classifier(features)
        if self.act is not None:
            out = self.act(out)

            if isinstance(self.act, nn.Sigmoid):
                out = out * self.output_range + self.output_shift

        return features,out