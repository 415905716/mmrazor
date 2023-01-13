# Copyright (c) OpenMMLab. All rights reserved.
from typing import Dict, List, Optional, Tuple, Union

import torch
import torch.nn as nn
from mmengine.config import Config

try:
    from torch.ao.quantization import enable_fake_quant
    from torch.ao.quantization.fx import prepare
    from torch.ao.quantization.fx.graph_module import ObservedGraphModule
    from torch.ao.quantization.qconfig_mapping import QConfigMapping
    from torch.ao.quantization.quantize_fx import _fuse_fx
    from torch.fx.graph_module import GraphModule
    from torch.nn.intrinsic.qat import modules as qat_fused_modules
    from torch.nn.qat import modules as qat_modules
except ImportError:
    from mmrazor.utils import get_package_placeholder, get_placeholder
    GraphModule = get_placeholder('torch>=1.13')
    ObservedGraphModule = get_placeholder('torch>=1.13')
    enable_fake_quant = get_placeholder('torch>=1.13')
    prepare = get_placeholder('torch>=1.13')
    QConfigMapping = get_placeholder('torch>=1.13')
    _fuse_fx = get_placeholder('torch>=1.13')
    qat_fused_modules = get_package_placeholder('torch>=1.13')
    qat_modules = get_package_placeholder('torch>=1.13')

from mmrazor import digit_version
from mmrazor.models.task_modules.tracer.fx import (
    del_fakequant_after_function, del_fakequant_after_method,
    del_fakequant_after_module, del_fakequant_after_op,
    del_fakequant_before_function, del_fakequant_before_method,
    del_fakequant_before_module, del_fakequant_before_op)
from mmrazor.models.utils import str2class
from mmrazor.registry import MODELS
from mmrazor.structures.quantization import BackendConfigs, QConfigHander
from .base import BaseQuantizer

if digit_version(torch.__version__) >= digit_version('1.13.0'):
    SUPPORT_QAT_MODULES: Tuple = (
        qat_fused_modules.ConvBn1d, qat_fused_modules.ConvBn2d,
        qat_fused_modules.ConvBn3d, qat_fused_modules.ConvBnReLU1d,
        qat_fused_modules.ConvBnReLU2d, qat_fused_modules.ConvBnReLU3d,
        qat_fused_modules.ConvReLU1d, qat_fused_modules.ConvReLU2d,
        qat_fused_modules.ConvReLU3d, qat_fused_modules.LinearBn1d,
        qat_fused_modules.LinearReLU, qat_modules.Conv1d, qat_modules.Conv2d,
        qat_modules.Conv3d, qat_modules.Linear)

    MERGE_BN_MAPPINGS: Dict = {
        qat_fused_modules.ConvBn1d: qat_modules.Conv1d,
        qat_fused_modules.ConvBn2d: qat_modules.Conv2d,
        qat_fused_modules.ConvBn3d: qat_modules.Conv3d,
        qat_fused_modules.ConvBnReLU1d: qat_fused_modules.ConvReLU1d,
        qat_fused_modules.ConvBnReLU2d: qat_fused_modules.ConvReLU2d,
        qat_fused_modules.ConvBnReLU3d: qat_fused_modules.ConvReLU3d,
        qat_fused_modules.LinearBn1d: qat_modules.Linear
    }
else:
    SUPPORT_QAT_MODULES = ()
    MERGE_BN_MAPPINGS = {}


@MODELS.register_module()
class NativeQuantizer(BaseQuantizer):
    """Native class for quantizer.

    Args:
        global_qconfig (Union[Dict, Config]): Config for quantization details
            of weight and activation include observer, quantizer, and qscheme.
        no_observer_modules (Optional[List]): Modules don't need observer.
            To fit different backend, we need qconfig to determine the modules
            which don't need observer.
        tracer (Dict): Config for tracer to trace modules for torch fx .

    Raises:
        NotImplementedError: _description_

    Examples:
        >>> global_qconfig = dict(
        ...     w_observer=dict(type='mmrazor.PerChannelMinMaxObserver'),
        ...     a_observer=dict(type='mmrazor.MovingAverageMinMaxObserver'),
        ...     w_fake_quant=dict(type='mmrazor.FakeQuantize'),
        ...     a_fake_quant=dict(type='mmrazor.FakeQuantize'),
        ...     w_qscheme=dict(
        ...         qdtype='qint8', bit=8, is_symmetry=True,
        ...         is_symmetric_range=True),
        ...     a_qscheme=dict(
        ...         qdtype='quint8', bit=8, is_symmetry=True,
        ...         averaging_constant=0.1),
)
    """

    # backend: 'native'
    # support_w_modes = ['per_tensor', 'per_channel']
    # support_a_modes = ['per_tensor']

    def __init__(self,
                 global_qconfig: Union[Dict, Config],
                 no_observer_modules: Optional[List] = None,
                 tracer: Dict = dict(type='CustomTracer'),
                 extra_redundant_fakequants: Dict = dict(
                     extra_module_prev_wo_fakequant=tuple(),
                     extra_module_next_wo_fakequant=tuple(),
                     extra_function_prev_wo_fakequant=tuple(),
                     extra_function_next_wo_fakequant=tuple(),
                     extra_method_prev_wo_fakequant=tuple(),
                     extra_method_next_wo_fakequant=tuple(),
                     extra_op_prev_wo_fakequant=tuple(),
                     extra_op_next_wo_fakequant=tuple())):
        super().__init__(tracer)
        self.qconfig = QConfigHander(global_qconfig)
        if self.qconfig.w_qscheme.is_per_channel:
            w_mode = 'per_channel'
        else:
            w_mode = 'per_tensor'
        if self.qconfig.a_qscheme.is_per_channel:
            a_mode = 'per_channel'
        else:
            a_mode = 'per_tensor'
        assert w_mode in self.support_w_modes
        assert a_mode in self.support_a_modes

        self.qconfig_mapping = QConfigMapping().set_global(
            self.qconfig.convert())
        if no_observer_modules:
            self.no_observer_modules = str2class(no_observer_modules)
            for mod in self.no_observer_modules:
                self.qconfig_mapping.set_object_type(mod, None)
        else:
            self.no_observer_modules = no_observer_modules
        self.backend_config = BackendConfigs[self.backend]
        self.example_inputs = (torch.randn(1, 3, 224, 224), )

        self.extra_redundant_fakequants = extra_redundant_fakequants

    @property
    def backend(self):
        """tmp."""
        return 'native'

    @property
    def support_w_modes(self):
        """tmp."""
        return ['per_tensor', 'per_channel']

    @property
    def support_a_modes(self):
        """tmp."""
        return ['per_tensor']

    def prepare(self, model, graph_module):
        """prepare graph to ObservedGraphModule.

        Args:
            graph_module (_type_): GraphModules before fuse.

        Returns:
            ObservedGraphModule: GraphModules after fuse and observer.

        Notes:
            'graph_module' after '_fuse_fx()' function will fuse conv, BN, ReLU
            into modules in SUPPORT_QAT_MODULES.
            'graph_module' after 'prepare()' function will become observed.

        Notes:
            Keep `is_qat` is True is because in Pytorch when `is_qat` is false,
            the `_fuse_fx()` function only fuse module into `nn.Squential` ,
            but we need it to be fused into `SUPPORT_QAT_MODULES` type.
        """

        graph_module = _fuse_fx(
            graph_module=graph_module,
            is_qat=True,
            backend_config=self.backend_config)
        prepared = prepare(
            model=graph_module,
            qconfig_mapping=self.qconfig_mapping,
            is_qat=True,
            node_name_to_scope=self.tracer.node_name_to_scope,
            example_inputs=self.example_inputs,
            backend_config=self.backend_config)
        prepared = self.del_redundant_fakequant(prepared)

        return prepared

    def post_process_weight_fakequant(self,
                                      observed_module: ObservedGraphModule,
                                      keep_fake_quant: bool = False):
        """weight fake-quant for supported QAT modules.

        Args:
            observed_module (ObservedGraphModule): Modules after fused and
                observed.
            keep_fake_quant (bool, optional): Bool to determine whether to keep
            fake-quant op, depending on the backend. Defaults to False.

        Note:
            `post_process_weight_fakequant()` function is necessary that the
                `SUPPORT_QAT_MODULES` will be convert to normal modules, and
                BN will be really integrated into conv layers.
        """

        def traverse(module):
            for name, child in module.named_children():
                # Trace `SUPPORT_QAT_MODULES` recursively.
                if isinstance(child, SUPPORT_QAT_MODULES):
                    # We add w_fakequant once in case some ptq methods have
                    # specific operations such as Adaround. So we do Quantize
                    # to perform these operations and do dequantize to
                    # introduce quantization loss in advance.
                    weight_fakequant = child.weight_fake_quant
                    child.weight.data = weight_fakequant(child.weight.data)

                    # `to_float()` function fuse BN into conv or conv_relu, and
                    # also convert a qat module to a normal module.
                    # source url: torch.nn.intrinsic.qat.modules.conv_fused.py
                    float_child = child.to_float()

                    # This is decided by backend type, some backend need
                    # explicitly keep the fake quant structure, others don't.
                    # TODO add deploy doc link
                    if keep_fake_quant:
                        for m in float_child.modules():
                            setattr(m, 'qconfig', self.qconfig.convert())

                        if type(child) in MERGE_BN_MAPPINGS:
                            cls = MERGE_BN_MAPPINGS[type(child)]
                            new_child = cls.from_float(float_child)
                        else:
                            new_child = type(child).from_float(float_child)

                        new_child.weight_fake_quant(new_child.weight)
                    else:
                        new_child = float_child
                    setattr(module, name, new_child)
                else:
                    traverse(child)

        observed_module.apply(enable_fake_quant)
        traverse(observed_module)

    def prepare_for_mmdeploy(self, model: nn.Module, dummy_input: Tuple,
                             checkpoint: Optional[str]):
        """Prepare model to Observed_model."""
        raise NotImplementedError

    def del_redundant_fakequant(self, prepared: GraphModule):
        """delete redundant fakequant op in prepared model.

        Returns:
            prepared (GraphModule): prepared model after delete redundant
                fakequant op.

        Notes:
             We can configure different ways to delete redundant nodes:
                @property
                def module_prev_wo_fakequant(self):
                    return (torch.nn.ReLU6, torch.nn.Identity)
        """
        extra_module_prev_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_module_prev_wo_fakequant', tuple())
        prepared = del_fakequant_before_module(
            prepared,
            self.module_prev_wo_fakequant + extra_module_prev_wo_fakequant,
            inplace=True)

        extra_module_next_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_module_next_wo_fakequant', tuple())
        prepared = del_fakequant_after_module(
            prepared,
            self.module_next_wo_fakequant + extra_module_next_wo_fakequant,
            inplace=True)

        extra_function_prev_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_function_prev_wo_fakequant', tuple())
        prepared = del_fakequant_before_method(
            prepared,
            self.function_prev_wo_fakequant + extra_function_prev_wo_fakequant,
            inplace=True)

        extra_function_next_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_function_next_wo_fakequant', tuple())
        prepared = del_fakequant_after_method(
            prepared,
            self.function_next_wo_fakequant + extra_function_next_wo_fakequant,
            inplace=True)

        extra_method_prev_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_method_prev_wo_fakequant', tuple())
        prepared = del_fakequant_before_function(
            prepared,
            self.method_prev_wo_fakequant + extra_method_prev_wo_fakequant,
            inplace=True)

        extra_method_next_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_method_next_wo_fakequant', tuple())
        prepared = del_fakequant_after_function(
            prepared,
            self.method_next_wo_fakequant + extra_method_next_wo_fakequant,
            inplace=True)

        extra_op_prev_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_op_prev_wo_fakequant', tuple())
        prepared = del_fakequant_before_op(
            prepared,
            self.op_prev_wo_fakequant + extra_op_prev_wo_fakequant,
            inplace=True)

        extra_op_next_wo_fakequant = self.extra_redundant_fakequants.get(
            'extra_op_next_wo_fakequant', tuple())
        prepared = del_fakequant_after_op(
            prepared,
            self.op_next_wo_fakequant + extra_op_next_wo_fakequant,
            inplace=True)
        return prepared

    @property
    def module_prev_wo_fakequant(self):
        """tmp."""
        return tuple()

    @property
    def module_next_wo_fakequant(self):
        """tmp."""
        return tuple()

    @property
    def function_prev_wo_fakequant(self):
        """tmp."""
        return tuple()

    @property
    def function_next_wo_fakequant(self):
        """tmp."""
        return tuple()

    @property
    def method_prev_wo_fakequant(self):
        """tmp."""
        return tuple()

    @property
    def method_next_wo_fakequant(self):
        """tmp."""
        return tuple()

    @property
    def op_prev_wo_fakequant(self):
        """tmp."""
        return tuple()

    @property
    def op_next_wo_fakequant(self):
        """tmp."""
        return tuple()
