"""`/v1/templates/install` must seed the CALLER'S tenant (never a hardcoded/target one),
and the shipped virtualbank-receivables template must compile to the reviewed 21-type set.

Read-only over a fake World Model — asserts the wiring, no DB writes.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aml.cloud.templates import registry
from aml.cloud.template_routes import get_template_routes


class _FakeWM:
    """Records every register_type(tenant_id, kind, name) — no persistence."""
    def __init__(self):
        self.calls = []

    def register_type(self, tenant_id, kind, name, spec):
        self.calls.append((tenant_id, kind, name))
        return {"tenant_id": tenant_id, "kind": kind, "name": name, "type_id": f"{kind}:{name}"}


def _req(tenant_id, scopes=("*",)):
    return SimpleNamespace(state=SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tenant_id, scopes=list(scopes)) if tenant_id else None))


def _install_ep(router):
    for r in router.routes:
        if r.path == "/install" and "POST" in r.methods:
            return r.endpoint
    raise KeyError("install endpoint not found")


def _body(template_id):
    return SimpleNamespace(template_id=template_id)  # duck-types InstallTemplateRequest


def test_install_seeds_callers_tenant_not_hardcoded():
    wm = _FakeWM()
    router = get_template_routes(wm)
    install = _install_ep(router)
    # Endpoint takes (req, request); pass the reviewed template as the caller "t_caller_vb".
    from aml.cloud.template_routes import get_template_routes as _  # keep import graph explicit
    install(_body("virtualbank-receivables"), _req("t_caller_vb"))

    tenants = {t for t, _, _ in wm.calls}
    assert tenants == {"t_caller_vb"}          # ONLY the caller's tenant
    assert "tenant_001" not in tenants         # the old hardcoded target is gone


def test_install_requires_auth_context():
    wm = _FakeWM()
    install = _install_ep(get_template_routes(wm))
    with pytest.raises(HTTPException) as ei:
        install(_body("virtualbank-receivables"), _req(None))   # no tenant context
    assert ei.value.status_code == 401
    assert wm.calls == []                       # nothing written


def test_install_fails_closed_on_present_ctx_but_empty_tenant_id():
    # Authed context present (scopes pass require_scope) but tenant_id is blank ⇒ MUST
    # 401 and write nothing — never fall back to any default/hardcoded tenant.
    wm = _FakeWM()
    install = _install_ep(get_template_routes(wm))
    req = SimpleNamespace(state=SimpleNamespace(tenant=SimpleNamespace(tenant_id="", scopes=["*"])))
    with pytest.raises(HTTPException) as ei:
        install(_body("virtualbank-receivables"), req)
    assert ei.value.status_code == 401
    assert wm.calls == []


def test_receivables_template_compiles_to_21_types():
    wm = _FakeWM()
    install = _install_ep(get_template_routes(wm))
    install(_body("virtualbank-receivables"), _req("t_caller_vb"))

    kinds = [k for _, k, _ in wm.calls]
    assert kinds.count("object") == 9
    assert kinds.count("link") == 7
    assert kinds.count("action") == 5
    assert len(wm.calls) == 21
    names = {n for _, _, n in wm.calls}
    # names mirror the console vocabulary (Reputation / Decision-Trail)
    assert {"Agent", "Invoice", "Decision", "Outcome", "Client", "Counterparty"} <= names
    assert {"certify", "reject", "record_outcome"} <= names


def test_template_is_registered_and_loadable():
    ids = {t["id"] for t in registry.list_templates()}
    assert "virtualbank-receivables" in ids
    assert "OntologyTemplate" in registry.get_template("virtualbank-receivables")
