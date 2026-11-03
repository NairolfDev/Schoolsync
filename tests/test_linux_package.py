from pathlib import Path


def test_package_contains_same_example_configuration_as_repository():
    root = Path(__file__).resolve().parents[1]
    assert (root / "config.example.toml").read_bytes() == (root / "src/schulsync/config.example.toml").read_bytes()
