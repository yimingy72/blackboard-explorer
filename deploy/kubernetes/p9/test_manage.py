"""Maintenance must preserve scope and refuse pending or unreviewed work."""

import ast
import hashlib
import json
from copy import deepcopy
from xml.etree.ElementTree import tostring

import manage
import pytest
from minio.commonconfig import Tags


class FixtureSite:
    def __init__(self):
        self.replicas = dict.fromkeys(manage.WORKLOADS, 1)
        self.calls = []
        self.epoch = 8
        self.pending = False
        self.after_app_stop = False
        self.ids = {name: 1 for name in self.replicas}

    def protected(self):
        return {"secret": "identity:revision"}

    def volumes(self):
        return {"postgres": "original-pvc", "minio": "original-minio-pvc"}

    def pg_data(self):
        return {"system_id": "fixture", "sha256": "rows", "fence": self.epoch}

    def database(self):
        return {
            "pending": self.pending or (self.after_app_stop and not self.replicas["blackboard"]),
            "epoch": self.epoch,
            "holders": self.replicas["runtime"],
        }

    def snapshot(self):
        return {
            "cluster_uid": "cluster",
            "workloads": {
                name: {
                    "uid": name,
                    "spec_sha256": name,
                    "images": {"main": "fixture"},
                    "pod_uid": self.ids[name],
                }
                for name in self.replicas
            },
            "sandbox": "unchanged",
            "protected": self.protected(),
            "volumes": self.volumes(),
            "postgres": self.pg_data(),
            "minio": "unchanged",
            "activity": self.database(),
        }

    def set_replicas(self, name, before, after, _reviewed):
        assert self.replicas[name] == before
        self.calls.append((name, after))
        self.replicas[name] = after
        if after:
            self.ids[name] += 1
            if name == "runtime":
                self.epoch += 1

    def wait_stopped(self, name):
        assert self.replicas[name] == 0

    def wait_ready(self, name):
        assert self.replicas[name] == 1

    def workload(self, name):
        return {"spec": {"replicas": self.replicas[name]}}


@pytest.fixture(autouse=True)
def closed_browser(monkeypatch):
    monkeypatch.setattr(manage, "ensure_browser_closed", lambda: None)


def test_redeploy_rejects_drift_and_pending_work_before_any_mutation():
    site = FixtureSite()
    reviewed = manage.digest(site.snapshot())
    site.ids["blackboard"] += 1
    with pytest.raises(manage.MaintenanceError, match="Site changed"):
        manage.redeploy(site, reviewed)
    site.pending = True
    with pytest.raises(manage.MaintenanceError, match="pending or unresolved"):
        manage.redeploy(site, manage.digest(site.snapshot()))
    assert site.calls == []


def test_recheck_after_app_stop_preserves_existing_runtime_and_stores():
    site = FixtureSite()
    site.after_app_stop = True
    with pytest.raises(manage.MaintenanceError, match="pending or unresolved"):
        manage.redeploy(site, manage.digest(site.snapshot()), restart_stores=True)
    assert site.calls == [("blackboard", 0), ("blackboard", 1)]
    assert site.epoch == 8 and all(value == 1 for value in site.replicas.values())


@pytest.mark.parametrize("stores", [False, True])
def test_redeploy_preserves_original_specs_and_orders_dependencies(stores):
    site = FixtureSite()
    before = deepcopy(site.snapshot())
    evidence = manage.redeploy(site, manage.digest(before), restart_stores=stores)
    store_steps = [("minio", 0), ("postgres", 0), ("postgres", 1), ("minio", 1)] if stores else []
    assert site.calls == [
        ("blackboard", 0),
        ("runtime", 0),
        *store_steps,
        ("blackboard", 1),
        ("runtime", 1),
    ]
    assert evidence["after"]["activity"] == {"pending": False, "epoch": 9, "holders": 1}
    assert evidence["after"]["volumes"] == before["volumes"]


def test_failed_stop_does_not_reopen_an_unconfirmed_writer():
    site = FixtureSite()

    def not_exited(_name):
        raise manage.MaintenanceError("Original Pod has not exited")

    site.wait_stopped = not_exited
    with pytest.raises(manage.MaintenanceError, match="recovery could not establish"):
        manage.redeploy(site, manage.digest(site.snapshot()))
    assert site.calls == [("blackboard", 0)]
    assert site.replicas["runtime"] == 1


def test_minio_nonempty_tags_use_canonical_mapping_not_xml_serializer():
    readback = ast.parse(manage.APP_READBACK)
    helpers = [
        item
        for item in readback.body
        if isinstance(item, ast.FunctionDef) and item.name in {"encode", "digest"}
    ]
    namespace = {"hashlib": hashlib, "json": json, "tostring": tostring}
    exec(compile(ast.Module(body=helpers, type_ignores=[]), "<app-readback>", "exec"), namespace)
    first, second = Tags.new_bucket_tags(), Tags.new_bucket_tags()
    first.update({"a": "1", "b": "2"})
    second.update({"b": "2", "a": "1"})
    encode, digest = namespace["encode"], namespace["digest"]
    assert encode(first) == {"a": "1", "b": "2"}
    assert digest(encode(first)) == digest(encode(second))
