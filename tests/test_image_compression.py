import io
import os
import tempfile
import unittest
from PIL import Image

import bot


class TestImageCompression(unittest.TestCase):
    """Tests for image compression and animation preservation in WebP."""

    def test_animated_gif_to_animated_webp(self):
        # Create an in-memory animated GIF (3 frames, 1000x500)
        frames = [
            Image.new("RGB", (1000, 500), color="red"),
            Image.new("RGB", (1000, 500), color="green"),
            Image.new("RGB", (1000, 500), color="blue"),
        ]
        gif_buf = io.BytesIO()
        frames[0].save(
            gif_buf,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=150,
            loop=0,
        )
        gif_bytes = gif_buf.getvalue()

        # Open and save as WebP via bot._save_image_as_webp
        img = Image.open(io.BytesIO(gif_bytes))
        with tempfile.TemporaryDirectory() as tmpdir:
            dest_path = os.path.join(tmpdir, "animated.webp")
            new_w, new_h, is_anim = bot._save_image_as_webp(img, dest_path, max_dim=800, quality=80)

            self.assertTrue(is_anim)
            self.assertEqual(new_w, 800)
            self.assertEqual(new_h, 400)

            # Verify saved WebP is animated and has 3 frames
            saved_img = Image.open(dest_path)
            self.assertTrue(getattr(saved_img, "is_animated", False))
            self.assertEqual(getattr(saved_img, "n_frames", 1), 3)
            self.assertEqual(saved_img.size, (800, 400))

    def test_static_image_to_webp(self):
        # Create a static 600x400 PNG
        static_img = Image.new("RGB", (600, 400), color="yellow")
        png_buf = io.BytesIO()
        static_img.save(png_buf, format="PNG")
        png_bytes = png_buf.getvalue()

        img = Image.open(io.BytesIO(png_bytes))
        out_buf = io.BytesIO()
        new_w, new_h, is_anim = bot._save_image_as_webp(img, out_buf, max_dim=800, quality=80)

        self.assertFalse(is_anim)
        self.assertEqual(new_w, 600)
        self.assertEqual(new_h, 400)

        saved_img = Image.open(out_buf)
        self.assertFalse(getattr(saved_img, "is_animated", False))

    def test_compress_image_bytes_animated(self):
        # Test higher-level compress_image function
        frames = [
            Image.new("RGB", (200, 200), color="black"),
            Image.new("RGB", (200, 200), color="white"),
        ]
        gif_buf = io.BytesIO()
        frames[0].save(
            gif_buf,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )

        compressed_bytes = bot.compress_image(gif_buf.getvalue(), max_width=800)
        res_img = Image.open(io.BytesIO(compressed_bytes))
        self.assertTrue(getattr(res_img, "is_animated", False))
        self.assertEqual(getattr(res_img, "n_frames", 1), 2)


if __name__ == "__main__":
    unittest.main()
