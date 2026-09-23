"""Cálculos puros: calendário, estimativas, preço médio e sugestão de pagamento. Sem acesso à BD."""
import calendar
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("CAFE_TZ", "Europe/Lisbon"))
DIAS_MINIMOS_HISTORICO = 5  # dias úteis com dados antes de confiar na média real


def agora() -> datetime:
    return datetime.now(timezone.utc)


def local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def mes_de(dt: datetime) -> str:
    return local(dt).strftime("%Y-%m")


def limites_mes(mes: str) -> tuple[date, date]:
    ano, m = map(int, mes.split("-"))
    return date(ano, m, 1), date(ano, m, calendar.monthrange(ano, m)[1])


def dias_uteis(inicio: date, fim: date) -> int:
    """Dias úteis (seg–sex) no intervalo fechado [inicio, fim]."""
    if fim < inicio:
        return 0
    n = 0
    d = inicio
    while d <= fim:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def ritmo_diario(cafes: int, dias_decorridos: int, cafes_dia_declarado: float) -> float:
    """Cafés por dia útil: média real se houver histórico suficiente, senão a previsão declarada."""
    if dias_decorridos >= DIAS_MINIMOS_HISTORICO:
        return cafes / dias_decorridos
    return cafes_dia_declarado


def dias_decorridos(mes: str, hoje: date, registado_em: date | None = None) -> int:
    """Dias úteis desde o dia 1 do mês (ou desde o registo da pessoa, se foi mais tarde) até hoje."""
    inicio, _ = limites_mes(mes)
    if registado_em and registado_em > inicio:
        inicio = registado_em
    return dias_uteis(inicio, hoje)


def arredonda(x: float) -> int:
    """Ao inteiro mais próximo, 0,5 sobe (o round() do Python vai ao par)."""
    return int(x + 0.5)


def preco_corrente(por_recuperar_cent: int, stock: int, ultimo_preco_cent: int) -> int:
    """Preço de um café = custo médio do que está no armário. Com o armário
    vazio (ou negativo) não há média: usa-se o preço do último café gravado.
    Com por_recuperar >= 0 e o 0,5 a subir, o último café leva o que falta ao
    cêntimo, portanto o pote fecha exactamente a zero."""
    if stock <= 0:
        return ultimo_preco_cent
    return max(0, arredonda(por_recuperar_cent / stock))


def sugestao_pagamento(meu_saldo_cent: int, outros: list[tuple[int, str, int]]) -> dict | None:
    """A quem pagar e quanto, só para quem deve. `outros` são (id, nome,
    saldo_cent) das restantes pessoas. Credor = o maior saldo positivo (em
    empate, o nome por ordem alfabética); o valor não passa do que o credor tem
    a receber, para várias pessoas não pagarem todas à mesma."""
    if meu_saldo_cent >= 0:
        return None
    credores = [o for o in outros if o[2] > 0]
    if not credores:
        return None
    uid, nome, saldo = min(credores, key=lambda o: (-o[2], o[1].casefold()))
    return {"utilizador_id": uid, "nome": nome, "valor_cent": min(-meu_saldo_cent, saldo)}


def estimativa_mes(cafes: int, hoje: date, mes: str, cafes_dia_declarado: float,
                   registado_em: date | None = None) -> int:
    """Cafés previstos até ao fim do mês: os já bebidos + ritmo × dias úteis que faltam."""
    _, fim = limites_mes(mes)
    decorridos = dias_decorridos(mes, hoje, registado_em)
    restantes = dias_uteis(hoje + timedelta(days=1), fim)
    return arredonda(cafes + ritmo_diario(cafes, decorridos, cafes_dia_declarado) * restantes)


def data_fim_stock(stock: int, ritmo_escritorio: float, hoje: date) -> date | None:
    """Dia em que o stock chega a zero ao ritmo actual (só conta dias úteis). None se o ritmo é 0."""
    if ritmo_escritorio <= 0:
        return None
    if stock <= 0:
        return hoje
    restante = float(stock)
    d = hoje
    while True:
        d += timedelta(days=1)
        if d.weekday() < 5:
            restante -= ritmo_escritorio
            if restante <= 0:
                return d


def resumo_stock(stock: int, ritmo_escritorio: float, hoje: date, limiar: int) -> dict:
    fim_mes = limites_mes(hoje.strftime("%Y-%m"))[1]
    acaba = data_fim_stock(stock, ritmo_escritorio, hoje)
    return {
        "stock": stock,
        "baixo": stock <= limiar,
        "limiar": limiar,
        "ritmo_dia": round(ritmo_escritorio, 2),
        "acaba_em": acaba.isoformat() if acaba else None,
        "chega_ao_fim_do_mes": acaba is None or acaba > fim_mes,
    }
