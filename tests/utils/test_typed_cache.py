from pathlib import Path

from pydantic import BaseModel

from src.miner.utils.cache import AgentCache, load_agent_cache


class Value(BaseModel):
    name: str


def test_typed_cache_uses_pure_acceptance(tmp_path: Path):
    cache = AgentCache("RCA", tmp_path, runtime="runtime", model="model")
    cache.set(Value(name="accepted"))
    calls = []

    def validate(value: Value):
        calls.append(value.name)
        return ()

    assert load_agent_cache(cache, Value, validate, label="RCA") == Value(name="accepted")
    assert calls == ["accepted"]
