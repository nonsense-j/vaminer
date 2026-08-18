import pytest

from src.miner.main import parse_args


@pytest.mark.parametrize("runtime", ["pydanic-sdk", "claude-cli"])
def test_cli_has_one_runtime_and_no_phase_override(runtime: str):
    args = parse_args(["--runtime", runtime, "CVE-2099-0001"])
    assert args.runtime == runtime
    assert not hasattr(args, "phase_runtime")
    with pytest.raises(SystemExit):
        parse_args(["--phase-runtime", "rca=claude-cli", "CVE-2099-0001"])
