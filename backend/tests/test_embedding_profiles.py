import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from app.routers.services import llm_providers, rag_engine, vision


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def _tei_env(dim="768"):
    return {
        "TEI_EMBED_BASE_URL": "http://embed.internal:8080/",
        "TEI_EMBED_MODEL": "nomic-ai/nomic-embed-text-v1.5",
        "TEI_EMBED_REVISION": "e9b6763023c676ca8431644204f50c2b100d9aab",
        "TEI_EMBED_DIM": dim,
        "TEI_EMBED_BATCH": "16",
    }


class EmbeddingProfileTests(unittest.TestCase):
    def test_default_profile_is_pinned_nomic_768(self):
        with patch.dict(
            os.environ,
            {
                "TEI_EMBED_MODEL": "",
                "TEI_EMBED_REVISION": "",
                "TEI_EMBED_DIM": "",
            },
            clear=False,
        ):
            profile = llm_providers.embedding_profile()
        self.assertEqual(profile["provider"], "tei")
        self.assertEqual(profile["model"], "nomic-ai/nomic-embed-text-v1.5")
        self.assertEqual(
            profile["revision"],
            "e9b6763023c676ca8431644204f50c2b100d9aab",
        )
        self.assertEqual(profile["dimension"], 768)
        self.assertEqual(profile["input_policy"], "nomic-v1.5-search-prefixes-v1")
        self.assertTrue(profile["normalize"])
        self.assertTrue(profile["truncate"])
        self.assertEqual(profile["truncation_direction"], "right")
        self.assertEqual(profile["dtype"], "float32")
        self.assertEqual(profile["pooling"], "mean")

    def test_tei_query_uses_native_payload_and_query_prefix(self):
        body = [[0.25, 0.75]]
        with patch.dict(os.environ, _tei_env("2"), clear=False), patch.object(
            llm_providers.urllib.request,
            "urlopen",
            return_value=_FakeResponse(body),
        ) as opened, patch.object(llm_providers, "_record_usage"):
            matrix = llm_providers.embed_query("which bridge?")

        self.assertEqual(matrix.shape, (1, 2))
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://embed.internal:8080/embed")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["inputs"], ["search_query: which bridge?"])
        self.assertTrue(payload["normalize"])
        self.assertTrue(payload["truncate"])
        self.assertEqual(payload["truncation_direction"], "right")
        self.assertEqual(payload["dimensions"], 2)

    def test_tei_passages_use_document_prefix(self):
        body = [[1, 0], [0, 1]]
        with patch.dict(os.environ, _tei_env("2"), clear=False), patch.object(
            llm_providers.urllib.request,
            "urlopen",
            return_value=_FakeResponse(body),
        ) as opened, patch.object(llm_providers, "_record_usage"):
            matrix = llm_providers.embed_passages(["first", "second"])

        self.assertEqual(matrix.shape, (2, 2))
        payload = json.loads(opened.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(
            payload["inputs"],
            ["search_document: first", "search_document: second"],
        )

    def test_provider_dimension_mismatch_is_rejected(self):
        with patch.dict(os.environ, _tei_env("3"), clear=False), patch.object(
            llm_providers.urllib.request,
            "urlopen",
            return_value=_FakeResponse([[0.25, 0.75]]),
        ), patch.object(llm_providers, "_record_usage"):
            with self.assertRaisesRegex(RuntimeError, "shape mismatch"):
                llm_providers.embed_passages(["stored chunk"])

    def test_readiness_verifies_model_revision_and_shape(self):
        profile = _tei_env("2")
        responses = [
            _FakeResponse(
                {
                    "model_id": profile["TEI_EMBED_MODEL"],
                    "model_sha": profile["TEI_EMBED_REVISION"],
                    "model_dtype": "float32",
                    "model_type": {"embedding": {"pooling": "mean"}},
                }
            ),
            _FakeResponse([[0.25, 0.75]]),
        ]
        with patch.dict(os.environ, profile, clear=False), patch.object(
            llm_providers.urllib.request,
            "urlopen",
            side_effect=responses,
        ), patch.object(llm_providers, "_record_usage"):
            result = llm_providers.verify_embedding_backend()
        self.assertEqual(result["dimension"], 2)
        self.assertEqual(result["model"], profile["TEI_EMBED_MODEL"])

    def test_readiness_rejects_wrong_revision(self):
        with patch.dict(os.environ, _tei_env("2"), clear=False), patch.object(
            llm_providers.urllib.request,
            "urlopen",
            return_value=_FakeResponse(
                {
                    "model_id": "nomic-ai/nomic-embed-text-v1.5",
                    "model_sha": "wrong",
                    "model_dtype": "float32",
                    "model_type": {"embedding": {"pooling": "mean"}},
                }
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "revision mismatch"):
                llm_providers.verify_embedding_backend()

    def test_legacy_profile_is_never_assumed_compatible(self):
        with patch.dict(os.environ, _tei_env(), clear=False):
            active = llm_providers.embedding_profile()
            self.assertFalse(llm_providers.embedding_profile_matches(None))
            self.assertFalse(
                llm_providers.embedding_profile_matches(
                    {
                        "provider": "tei",
                        "model": active["model"],
                        "dimension": 768,
                    }
                )
            )
            self.assertTrue(llm_providers.embedding_profile_matches(dict(active)))
            stale = dict(active)
            stale["revision"] = "old-revision"
            self.assertFalse(llm_providers.embedding_profile_matches(stale))

    def test_retrieval_falls_back_when_widths_disagree(self):
        chunks = [
            {"text": "deck corrosion finding", "page": 1},
            {"text": "unrelated inventory", "page": 2},
        ]
        roles = []

        def fake_embed(_texts, input_type="passage"):
            roles.append(input_type)
            return np.zeros((1, 2), dtype=np.float32)

        with patch.object(rag_engine, "_embed", side_effect=fake_embed):
            selected = rag_engine._get_relevant(
                "deck corrosion",
                chunks,
                k=1,
                embeddings=np.zeros((2, 3), dtype=np.float32),
            )
        self.assertEqual(selected, [chunks[0]])
        self.assertEqual(roles, ["query"])

    def test_manifest_matrix_requires_exact_profile_and_shape(self):
        with patch.dict(os.environ, _tei_env("2"), clear=False):
            entry = {
                "chunks": [{"text": "stored", "page": 1}],
                "image_descriptions": [],
                "embeddings": [[0.1, 0.2]],
                "image_embeddings": None,
                "embedding_profile": llm_providers.embedding_profile(),
            }
            self.assertTrue(rag_engine._entry_embeddings_compatible(entry))
            entry["embedding_profile"] = None
            self.assertFalse(rag_engine._entry_embeddings_compatible(entry))

    def test_lightrag_sets_passage_for_insert_and_query_for_lookup(self):
        roles = []

        class FakeRag:
            async def ainsert(self, _text):
                roles.append(("insert", rag_engine._KG_EMBED_INPUT_TYPE.get()))

            async def aquery(self, _question, param):
                roles.append(("query", rag_engine._KG_EMBED_INPUT_TYPE.get()))
                return "grounded answer"

        with patch.object(
            rag_engine, "_get_lightrag", new=AsyncMock(return_value=FakeRag())
        ):
            self.assertTrue(asyncio.run(rag_engine._build_kg("hash", "document")))
            self.assertEqual(
                asyncio.run(rag_engine._query_kg("question", "hash")),
                "grounded answer",
            )

        self.assertEqual(roles, [("insert", "passage"), ("query", "query")])

    def test_bridge_cache_marks_unprofiled_vectors_for_reembedding(self):
        sig = ((str(Path(vision.DATA_DIR) / "attachments" / "report.pdf"), 7),)
        payload = {
            "sig": vision._sig_str(sig),
            "chunks": [{"text": "parsed once", "page": 1, "kind": "text"}],
            "embeddings": [[0.1, 0.2]],
            "image_count": 0,
            "page_images": [],
        }
        cache_dir = MagicMock()
        cache_file = cache_dir.__truediv__.return_value
        cache_file.exists.return_value = True
        cache_file.read_text.return_value = json.dumps(payload)
        with patch.dict(os.environ, _tei_env("2"), clear=False), patch.object(
            vision, "_bridge_cache_dir", return_value=cache_dir
        ):
            loaded = vision._load_bridge_cache("34", "1", sig)

        self.assertIsNotNone(loaded)
        self.assertTrue(loaded["needs_reembedding"])
        self.assertIsNone(loaded["embeddings"])
        self.assertEqual(loaded["chunks"][0]["text"], "parsed once")


if __name__ == "__main__":
    unittest.main()
