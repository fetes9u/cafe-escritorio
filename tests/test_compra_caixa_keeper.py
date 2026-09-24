"""Só quem guarda a caixa pode registar ou trocar uma compra para paga pela
caixa: com MB WAY o dinheiro da caixa fica na conta de quem a guarda, por
isso só essa pessoa marca uma saída dela.
"""
from tests.conftest import regista
from tests.test_caixa import _alteracoes, _guarda, _ids

ONLY_KEEPER_PAYS_FROM_CAIXA = "Só quem guarda a caixa marca uma compra como paga pela caixa."


def _compras(sessao):
    return sessao.get("/api/escritorio").json()["compras"]


def test_non_keeper_post_compra_paga_pela_caixa_returns_403(cliente):
    ana = regista(cliente, "Ana")
    bea = regista(cliente, "Bea")
    ids = _ids(cliente)
    _guarda(ana, ids["Ana"])

    r = bea.post("/api/compras", json={"capsulas": 10, "custo_cent": 300, "paga_pela_caixa": True})

    assert r.status_code == 403
    assert r.json() == {"detail": ONLY_KEEPER_PAYS_FROM_CAIXA}
    assert _compras(bea) == []


def test_keeper_post_compra_paga_pela_caixa_returns_201(cliente):
    ana = regista(cliente, "Ana")
    ids = _ids(cliente)
    _guarda(ana, ids["Ana"])

    r = ana.post("/api/compras", json={"capsulas": 10, "custo_cent": 300, "paga_pela_caixa": True})

    assert r.status_code == 201
    assert _compras(ana)[0]["paga_pela_caixa"] is True


def test_non_keeper_post_compra_custo_zero_paga_pela_caixa_is_still_a_gift(cliente):
    ana = regista(cliente, "Ana")
    bea = regista(cliente, "Bea")
    ids = _ids(cliente)
    _guarda(ana, ids["Ana"])

    r = bea.post("/api/compras", json={"capsulas": 10, "custo_cent": 0, "paga_pela_caixa": True})

    assert r.status_code == 201
    assert _compras(bea)[0]["paga_pela_caixa"] is False


def test_non_keeper_patch_switch_to_caixa_returns_403_and_no_history(cliente):
    ana = regista(cliente, "Ana")
    bea = regista(cliente, "Bea")
    ids = _ids(cliente)
    _guarda(ana, ids["Ana"])
    bea.post("/api/compras", json={"capsulas": 10, "custo_cent": 300})
    compra = _compras(bea)[0]["id"]

    r = bea.patch(f"/api/compras/{compra}", json={"paga_pela_caixa": True})

    assert r.status_code == 403
    assert r.json() == {"detail": ONLY_KEEPER_PAYS_FROM_CAIXA}
    assert _alteracoes(bea, "compra", compra) == []
    assert _compras(bea)[0]["paga_pela_caixa"] is False


def test_keeper_patch_switch_to_caixa_returns_200(cliente):
    ana = regista(cliente, "Ana")
    ids = _ids(cliente)
    _guarda(ana, ids["Ana"])
    ana.post("/api/compras", json={"capsulas": 10, "custo_cent": 300})
    compra = _compras(ana)[0]["id"]

    r = ana.patch(f"/api/compras/{compra}", json={"paga_pela_caixa": True})

    assert r.status_code == 200
    assert _compras(ana)[0]["paga_pela_caixa"] is True


def test_non_keeper_author_can_still_edit_other_fields_of_a_caixa_compra(cliente):
    """A Ana regista pela caixa enquanto é responsável; a caixa passa depois
    para a Bea. A Ana continua a poder corrigir o custo da sua própria
    compra, mesmo já não guardando a caixa, porque não está a trocá-la para
    paga pela caixa (já o era)."""
    ana = regista(cliente, "Ana")
    bea = regista(cliente, "Bea")
    ids = _ids(cliente)
    _guarda(ana, ids["Ana"])
    ana.post("/api/compras", json={"capsulas": 10, "custo_cent": 300, "paga_pela_caixa": True})
    compra = _compras(ana)[0]["id"]
    _guarda(bea, ids["Bea"])

    r = ana.patch(f"/api/compras/{compra}", json={"custo_cent": 350})

    assert r.status_code == 200
    linha = _compras(ana)[0]
    assert linha["custo_cent"] == 350
    assert linha["paga_pela_caixa"] is True
