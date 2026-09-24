from pathlib import Path

from lawyer_agent.cli import corpus_publish_set as cli
from lawyer_agent.domain.legal_release_provenance import (
    MixedReleaseConfiguration,
    provenance_digest,
)
from lawyer_agent.infrastructure.documents.release_set_v3 import LoadedMixedReleaseSet
from tests.unit.test_mixed_release_quality import mixed_fixture


async def test_cli_uses_explicit_mixed_configuration_and_single_proof_reference():
    data, config, _, _ = await mixed_fixture()
    loaded = LoadedMixedReleaseSet(
        Path("C:/manifest.json"),
        config.selection_sha256,
        tuple(d.selection for d in data),
        config.provenance,
        "ds",
    )
    actual = cli.release_configuration(loaded, alias="laws", model_ref="fake", dimension=3)
    assert isinstance(actual, MixedReleaseConfiguration)
    assert actual == config
    fields = cli.release_provenance_fields(actual)
    assert set(fields) == {"release_selection_provenance"}
    assert fields["release_selection_provenance"] == dict(
        schema_version="release-provenance-ref-v1",
        manifest_sha256=config.selection_sha256,
        selection_sha256=config.provenance.selection_sha256,
        provenance_sha256=provenance_digest(config.provenance),
    )
