# Owner(s): ["module: dynamo"]

import os
import unittest

import torch
import torch._dynamo
import torch._dynamo.test_case

import torch._functorch._aot_autograd
from torch._dynamo import config as dynamo_config
from torch._dynamo.utils import counters
from torch._functorch import config as functorch_config
from torch._functorch._aot_autograd.autograd_cache import (
    AOTAutogradCache,
    autograd_cache_key,
    BypassAOTAutogradCache,
)
from torch._functorch._aot_autograd.schemas import AOTConfig
from torch._inductor import config as inductor_config
from torch.testing._internal.common_cuda import SM80OrLater
from torch.testing._internal.common_device_type import largeTensorTest
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
)
from torch.testing._internal.inductor_utils import GPU_TYPE, HAS_GPU


@instantiate_parametrized_tests
class AOTAutogradCacheTests(torch._dynamo.test_case.TestCase):
    def setUp(self):
        """
        Reset all counters and caches before each unit test
        """
        super().setUp()
        counters.clear()
        self._clear_all_caches()

    def _clear_all_caches(self):
        """
        Clear every cache, including AOTAutogradCache and FXCache
        """
        torch._inductor.codecache.FxGraphCache.clear()
        AOTAutogradCache.clear()
        self._clear_dynamo_and_codecache()

    def _clear_dynamo_and_codecache(self):
        """
        Clear unrelated caches, like dynamo and PyCodeCache
        """
        torch._dynamo.reset()
        for m in torch._inductor.codecache.PyCodeCache.cache.values():
            os.remove(m.__file__)
        torch._inductor.codecache.PyCodeCache.cache_clear()

    @inductor_config.patch("fx_graph_cache", True)
    @functorch_config.patch({"enable_autograd_cache": True})
    def test_basic(self):
        """
        Verify the interactions between FXGraphCache and AOTAutogradCache.
        """

        def fn(x, y):
            return (x * 2, y @ y)

        a = torch.rand(25)
        b = torch.rand(5, 5)

        compiled_fn = torch.compile(fn, backend="inductor")

        # A first call should miss in the cache.
        self.assertEqual(fn(a, b), compiled_fn(a, b))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)

        # A second call should hit. (First reset so in-memory guards
        # don't prevent compilation).
        self._clear_dynamo_and_codecache()
        self.assertEqual(fn(a, b), compiled_fn(a, b))

        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)

    @inductor_config.patch("fx_graph_cache", True)
    @functorch_config.patch({"enable_autograd_cache": True})
    def test_clear_fx_graph_cache(self):
        """
        Verify the interactions between FXGraphCache and AOTAutogradCache.
        """

        def fn(x, y):
            return (x * 2, y @ y)

        a = torch.rand(25)
        b = torch.rand(5, 5)

        compiled_fn = torch.compile(fn, backend="inductor")

        # A first call should miss in the cache.
        self.assertEqual(fn(a, b), compiled_fn(a, b))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)

        # Clear FX graph cache: second call should also be a miss
        self._clear_dynamo_and_codecache()
        torch._inductor.codecache.FxGraphCache.clear()
        self.assertEqual(fn(a, b), compiled_fn(a, b))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 2)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        # We save again into the cache
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 2)

    @inductor_config.patch("fx_graph_cache", False)
    @functorch_config.patch({"enable_autograd_cache": True})
    def test_fx_graph_cache_off(self):
        """
        Should not use cache if FXGraphCache is not enabled
        """

        def fn(x, y):
            return (x * 2, y @ y)

        a = torch.rand(25)
        b = torch.rand(5, 5)

        compiled_fn = torch.compile(fn, backend="inductor")

        # A first call should miss in the cache.
        self.assertEqual(fn(a, b), compiled_fn(a, b))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_bypass"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 0)

        # Clear FX graph cache: second call should also be a miss
        self._clear_dynamo_and_codecache()

        self.assertEqual(fn(a, b), compiled_fn(a, b))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_bypass"], 2)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 0)

    @inductor_config.patch("fx_graph_cache", True)
    @functorch_config.patch({"enable_autograd_cache": True})
    @dynamo_config.patch("compiled_autograd", True)
    def test_compiled_autograd_bypass(self):
        def fn(a, b):
            out = a.cos() + b
            loss = out.sum()
            ga, gb = torch.autograd.grad(loss, inputs=[a, b])

        a = torch.randn(25, requires_grad=True)
        b = torch.randn(25, requires_grad=True)
        a2 = a.detach().clone().requires_grad_(True)
        b2 = b.detach().clone().requires_grad_(True)
        compiled_fn = torch.compile(fn, backend="inductor")
        self.assertEqual(fn(a, b), compiled_fn(a2, b2))
        self.assertEqual(
            counters["aot_autograd"]["autograd_cache_miss"], 1
        )  # from compiled forward
        self.assertEqual(
            counters["aot_autograd"]["autograd_cache_bypass"], 1
        )  # from compiled autograd

    @inductor_config.patch("fx_graph_cache", True)
    @functorch_config.patch({"enable_autograd_cache": True})
    @dynamo_config.patch("compiled_autograd", True)
    def test_inference_graph_cache_hit_with_compiled_autograd_enabled(self):
        def fn(a, b):
            out = a.cos() + b
            return out.sum()

        a = torch.randn(25)
        b = torch.randn(25)
        compiled_fn = torch.compile(fn, backend="inductor")
        self.assertEqual(fn(a, b), compiled_fn(a, b))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)

        # Clear dynamo and run again. Should be a cache hit.
        counters.clear()
        self._clear_dynamo_and_codecache()
        self.assertEqual(fn(a, b), compiled_fn(a, b))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 0)

    @inductor_config.patch({"fx_graph_cache": True})
    @functorch_config.patch({"enable_autograd_cache": True})
    def test_autograd_lazy_backward(self):
        """
        Lazily compile the backward, and lazily save to cache
        """

        def fn(a, b):
            return a.cos() + b

        a = torch.randn(25, requires_grad=True)
        b = torch.randn(25, requires_grad=True)
        a2 = a.detach().clone().requires_grad_(True)
        b2 = b.detach().clone().requires_grad_(True)
        compiled_fn = torch.compile(fn, backend="inductor")
        self.assertEqual(fn(a, b), compiled_fn(a2, b2))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 0)

        # Clear dynamo and run again. Should be a cache miss still, because backward hasn't run
        self._clear_dynamo_and_codecache()
        self.assertEqual(fn(a, b), compiled_fn(a2, b2))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 2)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 0)

        # Now let's run the backward
        fn(a, b).sum().backward()
        compiled_fn(a2, b2).sum().backward()
        self.assertEqual(a.grad, a2.grad)
        self.assertEqual(b.grad, b2.grad)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)

        # Clear dynamo and rerun everything, now there should be a cache hit
        self._clear_dynamo_and_codecache()
        a = torch.randn(25, requires_grad=True)
        b = torch.randn(25, requires_grad=True)
        a2 = a.detach().clone().requires_grad_(True)
        b2 = b.detach().clone().requires_grad_(True)
        self.assertEqual(fn(a, b), compiled_fn(a2, b2))
        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 2)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)
        fn(a, b).sum().backward()
        compiled_fn(a2, b2).sum().backward()
        self.assertEqual(a.grad, a2.grad)
        self.assertEqual(b.grad, b2.grad)

    @inductor_config.patch("fx_graph_cache", True)
    @functorch_config.patch({"enable_autograd_cache": True})
    def test_autograd_function(self):
        """
        Tests autograd cache hits
        """

        def fn(a, b):
            return a.sin() + b

        a = torch.randn(25, requires_grad=True)
        b = torch.randn(25, requires_grad=True)
        a2 = a.detach().clone().requires_grad_(True)
        b2 = b.detach().clone().requires_grad_(True)

        compiled_fn = torch.compile(fn, backend="inductor")

        # A first call should miss in the cache.
        self.assertEqual(fn(a, b), compiled_fn(a2, b2))
        fn(a, b).sum().backward()
        compiled_fn(a2, b2).sum().backward()
        self.assertEqual(a.grad, a2.grad)
        self.assertEqual(b.grad, b2.grad)

        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 0)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)

        # Reset all tensors
        a = torch.randn(25, requires_grad=True)
        b = torch.randn(25, requires_grad=True)
        a2 = a.detach().clone().requires_grad_(True)
        b2 = b.detach().clone().requires_grad_(True)

        # A second call should hit. (First reset so in-memory guards
        # don't prevent compilation).
        self._clear_dynamo_and_codecache()
        self.assertEqual(fn(a, b), compiled_fn(a2, b2))
        fn(a, b).sum().backward()
        compiled_fn(a2, b2).sum().backward()
        self.assertEqual(a.grad, a2.grad)
        self.assertEqual(b.grad, b2.grad)

        self.assertEqual(counters["aot_autograd"]["autograd_cache_miss"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_hit"], 1)
        self.assertEqual(counters["aot_autograd"]["autograd_cache_saved"], 1)

    @largeTensorTest("64GB", device=GPU_TYPE)
    @parametrize("device", (GPU_TYPE,))
    @parametrize("dtype", (torch.float16, torch.bfloat16))
    @parametrize("requires_grad", (True, False))
    @inductor_config.patch("fx_graph_cache", True)
    @functorch_config.patch({"enable_autograd_cache": True})
    def test_autograd_inductor_guards(self, device, dtype, requires_grad):
        """
        Tests that functions that would add inductor guards are cached properly
        """
        if device == GPU_TYPE and not HAS_GPU:
            raise unittest.SkipTest(f"requires {GPU_TYPE}")
        if device == "cuda" and dtype == torch.bfloat16 and not SM80OrLater:
            raise unittest.SkipTest("requires CUDA SM80 or later")

        def fn(x, y):
            return (x + x, y + y)

        compiled_fn = torch.compile(fn, dynamic=True)

        # Iterate over different shapes, varying whether the total
        # size is below or above int32. For each combination, we expect
        # different guards around whether the symbolic sizes do or do
        # not exceed int32.
        shapes = (
            ((5, 6), (7, 8)),
            ((5, 6), (47000, 47001)),
            ((47000, 47001), (5, 6)),
        )
        expected_hits = expected_misses = expected_saves = 0
        for a_shape, b_shape in shapes:
            a = torch.rand(
                a_shape, device=device, dtype=dtype, requires_grad=requires_grad
            )
            b = torch.rand(
                b_shape, device=device, dtype=dtype, requires_grad=requires_grad
            )

            # AVOID a dynamo reset here. We expect guards to have been
            # added that will be violated with the new shape. We should
            # see a recompilation (along with a cache miss).
            res1 = compiled_fn(a, b)
            # A first call should miss in the cache.
            # NOTE: Currently, this cache miss is *not* due to guards,
            # but instead because the AOTAutogradCache key calculation specializes on input shapes.
            # Once we allow tensors with symints as part of the cache key calculation, it will
            # instead cache miss because of guard failure.
            expected_misses += 1

            self.assertEqual(
                counters["aot_autograd"]["autograd_cache_miss"], expected_misses
            )
            self.assertEqual(
                counters["aot_autograd"]["autograd_cache_hit"], expected_hits
            )
            # Because dynamic shapes are enabled, we expect backwards to be compiled ahead of time
            # So we should see a cache save here
            expected_saves += 1
            self.assertEqual(
                counters["aot_autograd"]["autograd_cache_saved"], expected_saves
            )
            if requires_grad:
                res1[0].sum().backward()
                # No extra saves
                self.assertEqual(
                    counters["aot_autograd"]["autograd_cache_saved"], expected_saves
                )

            a2 = a.detach().clone().requires_grad_(requires_grad)
            b2 = b.detach().clone().requires_grad_(requires_grad)
            # A second call should hit. (First reset so in-memory guards
            # don't prevent compilation).

            # Now clear dynamo and we should see a cache hit
            # This should populate guards to dynamo's cache, so that a subsequent run with a different
            # shape will still trigger a second call to autograd_cache.
            self._clear_dynamo_and_codecache()
            res2 = compiled_fn(a2, b2)
            expected_hits += 1
            self.assertEqual(
                counters["aot_autograd"]["autograd_cache_miss"], expected_misses
            )
            self.assertEqual(
                counters["aot_autograd"]["autograd_cache_hit"], expected_hits
            )
            self.assertEqual(
                counters["aot_autograd"]["autograd_cache_saved"], expected_saves
            )
            self.assertEqual(res1, res2)
            if requires_grad:
                res2[0].sum().backward()
                self.assertEqual(a.grad, a2.grad)


@inductor_config.patch("fx_graph_cache", True)
class AOTAutogradCachePicklerTests(torch._dynamo.test_case.TestCase):
    @property
    def device_type(self) -> str:
        return "cuda" if torch.cuda.is_available() else "cpu"

    def default_config(self):
        return AOTConfig(
            fw_compiler=None,
            bw_compiler=None,
            inference_compiler=None,
            partition_fn=None,
            decompositions={},
            num_params_buffers=0,
            aot_id=0,
            keep_inference_input_mutations=False,
            dynamic_shapes=True,
            aot_autograd_arg_pos_to_source=None,
            is_export=False,
            no_tangents=False,
            enable_log=False,
        )

    def _get_dynamo_output(self, fn, *args, **kwargs):
        # Reset dynamo between runs
        torch._dynamo.reset()
        fx_graph = None
        example_inputs = None

        def compiler(gm, inputs, **kwargs):
            nonlocal fx_graph
            nonlocal example_inputs
            fx_graph = gm
            example_inputs = inputs
            return gm

        g = torch.compile(fn, backend=compiler, fullgraph=True)
        result = g(*args, **kwargs)
        return (result, fx_graph, example_inputs)

    def gen_cache_key(self, f, config, inputs=None):
        if inputs is None:
            inputs = [torch.ones(3)]
        _, fx_g, example_inputs = self._get_dynamo_output(f, *inputs)
        return autograd_cache_key(fx_g, example_inputs, config)

    def test_basic_hash_key(self):
        def fn(x):
            return x.sin().cos()

        config = self.default_config()
        # Check hash is stable on multiple runs
        c1 = self.gen_cache_key(fn, config)
        c2 = self.gen_cache_key(fn, config)
        self.assertEqual(c1, c2)

    def test_identical_graphs_and_configs(self):
        def fn(x):
            return x.sin().cos()

        def fn2(x):
            y = x.sin()
            z = y.cos()
            return z

        # Make the id different, but otherwise identical
        config = self.default_config()
        config2 = self.default_config()
        config2.aot_id = 1

        c1 = self.gen_cache_key(fn, config)
        c2 = self.gen_cache_key(fn, config2)
        self.assertEqual(c1, c2)

    def test_different_graphs(self):
        def fn(x):
            return x.cos().sin()

        def fn2(x):
            return x.sin().cos()

        config = self.default_config()
        c1 = self.gen_cache_key(fn, config)
        c2 = self.gen_cache_key(fn2, config)
        self.assertNotEqual(c1, c2)

    def test_different_configs(self):
        def fn(x):
            return x.cos().sin()

        config = self.default_config()
        config2 = self.default_config()
        config2.dynamic_shapes = False
        c1 = self.gen_cache_key(fn, config)
        c2 = self.gen_cache_key(fn, config2)
        self.assertNotEqual(c1, c2)

    def test_different_inputs(self):
        def fn(x):
            return x.cos().sin()

        config = self.default_config()
        c1 = self.gen_cache_key(fn, config, inputs=[torch.ones(3)])
        c2 = self.gen_cache_key(fn, config, inputs=[torch.ones(2)])
        self.assertNotEqual(c1, c2)

    def test_different_global_configs(self):
        def fn(x):
            return x.cos().sin()

        config = self.default_config()

        c1 = self.gen_cache_key(fn, config)
        c2 = self.gen_cache_key(fn, config)
        self.assertEqual(c1, c2)

        c1 = self.gen_cache_key(fn, config)

        # Change functorch config
        with functorch_config.patch(
            {"debug_assert": not functorch_config.debug_assert}
        ):
            c2 = self.gen_cache_key(fn, config)

        self.assertNotEqual(c1, c2)

        c1 = self.gen_cache_key(fn, config)
        # Change inductor config
        with inductor_config.patch({"debug": not inductor_config.debug}):
            c2 = self.gen_cache_key(fn, config)

        self.assertNotEqual(c1, c2)

        c1 = self.gen_cache_key(fn, config)
        # Change torch grad enabled
        with torch.no_grad():
            c2 = self.gen_cache_key(fn, config)
        self.assertNotEqual(c1, c2)

    def test_incompatible_function(self):
        @torch._dynamo.allow_in_graph
        class AllowInGraphFunc(torch.autograd.Function):
            @staticmethod
            def forward(_, x):
                torch._dynamo.graph_break()
                return x.sin()

        def fn(x):
            return AllowInGraphFunc.apply(x)

        config = self.default_config()
        self.assertRaises(
            BypassAOTAutogradCache, lambda: self.gen_cache_key(fn, config)
        )

    def test_normal_torch_function(self):
        @torch._dynamo.allow_in_graph
        def fn(x):
            y = torch.sin(x)
            z = torch.cos(x)
            w = y + z
            w.abs()
            return w

        config = self.default_config()
        self.gen_cache_key(fn, config)


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    run_tests()
