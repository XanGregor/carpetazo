"""
Tests de extensions.py — solo la lógica pura de selección del límite
(_resolver_limite), sin levantar un execution_context real de Strawberry
ni una request HTTP. Que el límite resultante realmente se aplique durante
la validación de una query real (con el mensaje de error mostrando el
número correcto para CADA api_key) se probó a mano de punta a punta contra
el schema público montado en FastAPI — ver README, sección "Límite de
profundidad por API key": misma query, dos api keys con
limite_profundidad_query distinto (10 y 2), la de límite bajo la rechaza
con "exceeds maximum operation depth of 2" y la otra la deja pasar.
"""
from graphql_api.extensions import LIMITE_POR_DEFECTO, _resolver_limite


class _ApiKeyFalsa:
    """Duck-typing liviano: _resolver_limite solo lee .limite_profundidad_query,
    no hace falta la dataclass real auth.ApiKeyActual para este test."""

    def __init__(self, limite):
        self.limite_profundidad_query = limite


def test_usa_el_limite_de_la_api_key():
    assert _resolver_limite({"api_key": _ApiKeyFalsa(7)}) == 7


def test_sin_api_key_en_el_contexto_usa_el_default():
    assert _resolver_limite({}) == LIMITE_POR_DEFECTO


def test_contexto_none_usa_el_default():
    assert _resolver_limite(None) == LIMITE_POR_DEFECTO


def test_contexto_que_no_es_dict_usa_el_default():
    assert _resolver_limite(object()) == LIMITE_POR_DEFECTO


def test_limite_cero_usa_el_default():
    """Un límite en 0 en la tabla api_key sería un misconfig — mejor caer al
    default conservador que dejar pasar profundidad ilimitada."""
    assert _resolver_limite({"api_key": _ApiKeyFalsa(0)}) == LIMITE_POR_DEFECTO


def test_limite_negativo_usa_el_default():
    assert _resolver_limite({"api_key": _ApiKeyFalsa(-3)}) == LIMITE_POR_DEFECTO
