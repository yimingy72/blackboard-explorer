"""The two local assertion groups run in the pinned, patched official Server image."""

import unittest
from unittest.mock import MagicMock, patch

import httpx
from fastapi import HTTPException
from kubernetes.client import ApiException
from opensandbox_server.config import AppConfig, KubernetesRuntimeConfig, RuntimeConfig
from opensandbox_server.services.k8s.client import K8sClient
from opensandbox_server.services.k8s.informer import WorkloadInformer
from opensandbox_server.services.k8s.kubernetes_service import KubernetesSandboxService
from opensandbox_server.tenants.http_provider import (
    HTTPTenantProvider,
    HTTPTenantProviderConfig,
    _FlightEvent,
)
from opensandbox_server.tenants.models import TenantEntry
from opensandbox_server.tenants.provider import TenantProviderUnavailable


def provider(bound=32):
    result = HTTPTenantProvider(
        HTTPTenantProviderConfig(
            endpoint="http://authority.test/tenant", cache_max_entries=bound, max_stale_seconds=0
        )
    )
    calls = []

    def resolve(request):
        key = request.headers["OPEN-SANDBOX-API-KEY"]
        calls.append(key)
        if key == "revoked":
            return httpx.Response(401)
        if key == "unavailable":
            return httpx.Response(503)
        return httpx.Response(200, json={"namespace": "bbx-g7-ns-" + key.split("-")[-1], "ttl": 30})

    result._client = httpx.Client(transport=httpx.MockTransport(resolve))
    return result, calls


class ServerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_churn_and_authoritative_re_resolution(self):
        config = KubernetesRuntimeConfig(
            namespace="bbx-g7-default", batchsandbox_informer_enabled=False
        )
        with (
            patch.object(K8sClient, "_load_config"),
            patch.object(WorkloadInformer, "start") as start,
        ):
            client = K8sClient(config)
            api = MagicMock()
            api.get_namespaced_custom_object.side_effect = lambda **kw: {
                "metadata": {"name": kw["name"], "namespace": kw["namespace"]}
            }
            api.list_namespaced_custom_object.return_value = {"items": []}
            client._custom_objects_api = api
            client._read_limiter = MagicMock()
            tenants, calls = provider()
            with patch("opensandbox_server.tenants.http_provider.time.monotonic", return_value=100):
                for index in range(1000):
                    namespace = "bbx-g7-ns-" + str(index)
                    self.assertEqual(tenants.lookup("key-" + str(index)).namespace, namespace)
                    self.assertLessEqual(len(tenants._cache), 32)
                    client.get_custom_object(
                        "sandbox.opensandbox.io", "v1alpha1", namespace, "batchsandboxes", "sandbox"
                    )
                    client.list_custom_objects(
                        "sandbox.opensandbox.io",
                        "v1alpha1",
                        namespace,
                        "batchsandboxes",
                        "bbx-task=x",
                    )
                    self.assertIsNone(
                        client.subscribe_custom_objects(
                            "sandbox.opensandbox.io",
                            "v1alpha1",
                            namespace,
                            "batchsandboxes",
                            ["sandbox"],
                            lambda *_: None,
                        )
                    )
                self.assertEqual(len(client._informers), 0)
                self.assertEqual(client._read_limiter.acquire.call_count, 2000)
                start.assert_not_called()
                self.assertNotIn("key-0", tenants._cache)
                self.assertEqual(tenants.lookup("key-0").namespace, "bbx-g7-ns-0")
                self.assertEqual(calls.count("key-0"), 2)
                self.assertLessEqual(len(tenants.list_tenants()), 32)
            for call in api.get_namespaced_custom_object.call_args_list:
                self.assertEqual(call.kwargs["_request_timeout"], (10, 30))
                self.assertTrue(call.kwargs["namespace"].startswith("bbx-g7-ns-"))
            self.assertEqual(
                api.list_namespaced_custom_object.call_args.kwargs["label_selector"], "bbx-task=x"
            )
            api.get_namespaced_custom_object.side_effect = ApiException(status=404)
            self.assertIsNone(
                client.get_custom_object(
                    "sandbox.opensandbox.io", "v1alpha1", "gone", "batchsandboxes", "sandbox"
                )
            )
            api.get_namespaced_custom_object.side_effect = ApiException(status=503)
            with self.assertRaises(ApiException):
                client.get_custom_object(
                    "sandbox.opensandbox.io", "v1alpha1", "gone", "batchsandboxes", "sandbox"
                )
            with patch("opensandbox_server.tenants.http_provider.time.monotonic", return_value=131):
                self.assertEqual(tenants.list_tenants(), [])
            self.assertIsNone(tenants.lookup("revoked"))
            with self.assertRaises(TenantProviderUnavailable):
                tenants.lookup("unavailable")
            flight = _FlightEvent()
            flight.result = TenantEntry(
                name="bbx-g7-ns-active", namespace="bbx-g7-ns-active", api_keys=("active",)
            )
            flight.set()
            tenants._inflight["active"] = flight
            self.assertEqual(tenants._fetch_and_cache("active", 132), flight.result)
            self.assertNotIn("active", tenants._cache)
            tenants.close()
            legacy, _ = provider(0)
            with patch("opensandbox_server.tenants.http_provider.time.monotonic", return_value=100):
                for index in range(40):
                    legacy.lookup("key-" + str(index))
            with patch(
                "opensandbox_server.tenants.http_provider.time.monotonic", return_value=1000
            ):
                self.assertEqual(len(legacy.list_tenants()), 40)
            legacy.close()
            default = K8sClient(KubernetesRuntimeConfig(namespace="bbx-g7-default"))
            default._custom_objects_api = api
            default.subscribe_custom_objects(
                "sandbox.opensandbox.io",
                "v1alpha1",
                "default",
                "batchsandboxes",
                ["sandbox"],
                lambda *_: None,
            )
            self.assertEqual(start.call_count, 1)
            default.stop_informers()
            # Required reactors for other GVRs remain available in direct Batch mode.
            client.watch_custom_objects(
                "sandbox.opensandbox.io", "v1alpha1", "static", "sandboxsnapshots", lambda *_: None
            )
            self.assertEqual(start.call_count, 2)
            client.stop_informers()

    async def test_creation_polling_faults_and_background_lookup_after_eviction(self):
        config = AppConfig(
            runtime=RuntimeConfig(type="kubernetes", execd_image="test-execd"),
            kubernetes=KubernetesRuntimeConfig(
                namespace="bbx-g7-default", batchsandbox_informer_enabled=False
            ),
        )
        with patch.object(K8sClient, "_load_config"):
            service = KubernetesSandboxService(config)
        service.workload_provider = MagicMock()
        service.workload_provider.subscribe_workload.return_value = None
        pending = {"metadata": {"name": "sandbox", "uid": "uid-1"}}
        ready = {"metadata": {"name": "sandbox", "uid": "uid-2"}}
        service.workload_provider.get_workload.side_effect = [pending, None, ready]
        service.workload_provider.get_status.side_effect = [
            {"state": "Pending", "reason": None, "message": None},
            {"state": "Running", "reason": None, "message": None},
        ]
        result = await service._wait_for_sandbox_ready(
            "sandbox", timeout_seconds=1, poll_interval_seconds=0.001
        )
        self.assertEqual(result, ready)
        service.workload_provider.create_workload.assert_not_called()
        service.workload_provider.get_workload.side_effect = ApiException(status=503)
        with (
            patch("opensandbox_server.services.k8s.kubernetes_service.logger.warning"),
            self.assertRaises(HTTPException) as failure,
        ):
            await service._wait_for_sandbox_ready(
                "sandbox", timeout_seconds=0.02, poll_interval_seconds=0.005
            )
        self.assertEqual(failure.exception.status_code, 504)
        self.assertEqual(failure.exception.detail["code"], "KUBERNETES::POD_READY_TIMEOUT")
        service.workload_provider.create_workload.assert_not_called()
        tenants, _ = provider(1)
        tenants.lookup("key-0")
        tenants.lookup("key-1")
        self.assertNotIn("key-0", tenants._cache)
        service._tenant_provider = tenants
        service.workload_provider.get_workload.side_effect = None
        service.workload_provider.get_workload.return_value = None
        service.workload_provider.list_workloads_all_namespaces.return_value = [
            {"metadata": {"namespace": "bbx-g7-ns-0"}}
        ]
        self.assertEqual(service._find_sandbox_namespace("active-sandbox"), "bbx-g7-ns-0")
        service.workload_provider.list_workloads_all_namespaces.assert_called_once_with(
            "opensandbox.io/id=active-sandbox"
        )
        tenants.close()


if __name__ == "__main__":
    unittest.main()
