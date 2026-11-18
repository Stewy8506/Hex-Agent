"""Multimodal Vision Engine for HEX Agent.

Sends the real image bytes as a base64 multimodal content block through the
model router. When no multimodal route is available the engine fails explicitly
with the image metadata it did read, rather than inventing an analysis (B51).
Supports local image files (PNG, JPG, WEBP, GIF, SVG) and remote HTTP/HTTPS image URLs.
"""

from __future__ import annotations

import base64
import html
import logging
import mimetypes
import os
import struct
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_EXTENSIONS = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".bmp",
})


def get_image_metadata(data: bytes, ext: str) -> tuple[int, int] | None:
    """Extract width and height from raw image bytes without external heavy dependencies."""
    try:
        if ext == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h)
        elif ext in (".jpg", ".jpeg") and len(data) >= 2 and data[:2] == b"\xff\xd8":
            # Quick JPEG marker scan for SOF0/SOF2
            idx = 2
            while idx < len(data) - 9:
                if data[idx] == 0xFF and data[idx + 1] in (0xC0, 0xC2):
                    h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                    return (w, h)
                elif data[idx] == 0xFF:
                    seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
                    idx += 2 + seg_len
                else:
                    idx += 1
        elif ext == ".gif" and len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return (w, h)
    except Exception:
        pass
    return None


class VisionEngine:
    """Multimodal image inspection engine for HEX Agent."""

    def __init__(self, router: Any | None = None) -> None:
        self.router = router

    def inspect_image(
        self,
        image_path_or_url: str,
        prompt: str,
        detail: str = "auto",
        workspace_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Inspect a local file or remote image URL with user prompt."""
        target = image_path_or_url.strip()
        data_bytes: bytes = b""
        mime_type = "image/png"
        source_label = target

        try:
            if target.startswith(("http://", "https://")):
                req = urllib.request.Request(
                    target,
                    headers={"User-Agent": "HEX-Agent/1.0 (Vision)"},
                )
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    data_bytes = resp.read()
                    mime_type = resp.headers.get_content_type() or "image/png"
            else:
                # Local file
                img_path = Path(target)
                if not img_path.is_absolute() and workspace_dir is not None:
                    img_path = Path(workspace_dir) / img_path
                img_path = img_path.resolve()

                if not img_path.exists():
                    return {"ok": False, "error": f"Image file not found: {img_path}"}
                if not img_path.is_file():
                    return {"ok": False, "error": f"Path is not a regular file: {img_path}"}

                ext = img_path.suffix.lower()
                if ext not in SUPPORTED_IMAGE_EXTENSIONS:
                    return {
                        "ok": False,
                        "error": f"Unsupported image extension '{ext}'. Supported: {', '.join(sorted(SUPPORTED_IMAGE_EXTENSIONS))}",
                    }

                mime_type = mimetypes.guess_type(str(img_path))[0] or f"image/{ext.lstrip('.')}"
                data_bytes = img_path.read_bytes()
                source_label = str(img_path)

            size_bytes = len(data_bytes)
            if size_bytes == 0:
                return {"ok": False, "error": "Image file is empty (0 bytes)"}

            # Extract dimensions
            ext = "." + mime_type.split("/")[-1]
            dimensions = get_image_metadata(data_bytes, ext)
            dim_str = f"{dimensions[0]}x{dimensions[1]}" if dimensions else "unknown"

            metadata = {
                "source": source_label,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "dimensions": dimensions,
                "detail": detail,
                "pixels_sent": False,
                "resolution": dim_str,
            }

            def _unavailable(reason: str) -> dict[str, Any]:
                # B51: never invent an analysis when no multimodal route ran.
                return {
                    "ok": False,
                    "error": f"Vision backend unavailable: {reason}",
                    **metadata,
                }

            if self.router is None or not hasattr(self.router, "execute_routed_request"):
                return _unavailable(
                    "no multimodal model router is configured; the image was not analysed"
                )

            # Send the actual pixels as a base64 data URI content block.
            b64_data = base64.b64encode(data_bytes).decode("ascii")
            data_uri = f"data:{mime_type};base64,{b64_data}"
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_uri, "detail": detail},
                        },
                    ],
                }
            ]
            try:
                decision, model_resp = self.router.execute_routed_request(
                    prompt=f"Analyse the attached image. {prompt}",
                    task_class="research",
                    risk_class="R1",
                    messages=messages,
                )
            except TypeError as exc:
                return _unavailable(
                    f"the configured router does not accept multimodal content blocks ({exc})"
                )
            except Exception as exc:
                logger.warning("Multimodal vision request failed for %s: %s", source_label, exc)
                return _unavailable(str(exc))

            tier = str(getattr(decision, "tier", "") or "")
            if tier != "tier2_cloud_reasoner":
                return _unavailable(
                    f"request routed to '{tier or 'unknown'}', a text-only tier that cannot receive image pixels"
                )

            analysis_text = (
                getattr(model_resp, "content", None)
                or getattr(model_resp, "text", None)
                if model_resp is not None
                else None
            )
            if not analysis_text:
                return _unavailable("the multimodal model returned no analysis")

            return {
                "ok": True,
                "analysis": analysis_text,
                **metadata,
                "pixels_sent": True,
            }

        except Exception as exc:
            logger.exception("Failed to inspect image %s: %s", target, exc)
            return {"ok": False, "error": str(exc)}
