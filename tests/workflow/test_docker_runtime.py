from pathlib import Path


def test_workflow_runtime_includes_github_cli() -> None:
    dockerfile = (Path(__file__).parents[2] / "Dockerfile").read_text(
        encoding="utf-8"
    )
    install_block = dockerfile.split("install -y --no-install-recommends", 1)[1]
    install_block = install_block.split("&&", 1)[0]

    assert " gh " in f" {install_block} "
