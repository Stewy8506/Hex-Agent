"""B51 regression: vision sends real pixels or fails explicitly.

The engine must transmit the actual base64 image bytes in a multimodal content
block, accept an answer only from a multimodal (cloud) route, and never invent
an analysis when that route is unavailable.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness_core.capabilities.vision import VisionEngine

TINY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


class RecordingRouter:
    """Router stub that records the request and returns a scripted outcome."""

    def __init__(
        self,
        tier: str = "tier2_cloud_reasoner",
        content: str = "A 1x1 transparent pixel.",
    ) -> None:
        self.tier = tier
        self.content = content
        self.kwargs: dict = {}

    def execute_routed_request(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(tier=self.tier), SimpleNamespace(content=self.content)


class TestVisionPixels(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.ws = Path(self.temp_dir)
        self.image = self.ws / "pixel.png"
        self.image.write_bytes(TINY_PNG_BYTES)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_real_pixels_are_sent_as_multimodal_content(self) -> None:
        router = RecordingRouter()
        res = VisionEngine(router=router).inspect_image(
            str(self.image), prompt="What is in this image?", workspace_dir=self.ws
        )

        self.assertTrue(res["ok"])
        self.assertTrue(res["pixels_sent"])
        self.assertEqual(res["analysis"], "A 1x1 transparent pixel.")

        messages = router.kwargs["messages"]
        blocks = messages[0]["content"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[1]["type"], "image_url")
        data_uri = blocks[1]["image_url"]["url"]
        self.assertTrue(data_uri.startswith("data:image/png;base64,"))
        # The transmitted payload is the complete file, not a truncated stub.
        self.assertEqual(
            base64.b64decode(data_uri.split(",", 1)[1]),
            TINY_PNG_BYTES,
        )

    def test_text_only_route_is_rejected(self) -> None:
        router = RecordingRouter(tier="tier1_local_m4")
        res = VisionEngine(router=router).inspect_image(
            str(self.image), prompt="Describe", workspace_dir=self.ws
        )
        self.assertFalse(res["ok"])
        self.assertIn("text-only tier", res["error"])
        self.assertFalse(res["pixels_sent"])

    def test_empty_model_response_fails(self) -> None:
        res = VisionEngine(router=RecordingRouter(content="")).inspect_image(
            str(self.image), prompt="Describe", workspace_dir=self.ws
        )
        self.assertFalse(res["ok"])
        self.assertIn("no analysis", res["error"])

    def test_router_rejecting_multimodal_content_fails(self) -> None:
        class LegacyRouter:
            def execute_routed_request(self, **kwargs):
                raise TypeError("unexpected keyword argument 'messages'")

        res = VisionEngine(router=LegacyRouter()).inspect_image(
            str(self.image), prompt="Describe", workspace_dir=self.ws
        )
        self.assertFalse(res["ok"])
        self.assertIn("multimodal content blocks", res["error"])


if __name__ == "__main__":
    unittest.main()
