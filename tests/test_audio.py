"""
Tests for audio MIME detection, 20MB file limits, Gemini audio summarization,
and voice/audio message support in /ai and /tldr commands.
"""
import os
import sys
import json
import unittest
import tempfile
from unittest.mock import MagicMock, patch

_TEST_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TEST_DB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database

database.init_db()


class MockEvent:
    def __init__(self, from_id=10, chat_id=1, msg_id=100, payload="", text="", quote=None, file=None):
        self.msg = MagicMock()
        self.msg.from_id = from_id
        self.msg.chat_id = chat_id
        self.msg.id = msg_id
        self.msg.text = text
        self.msg.file = file
        self.msg.is_bot = False
        self.msg.is_info = False
        self.msg.quote = quote
        self.msg.file_bytes = 0
        self.msg.view_type = ""
        self.msg.file_name = ""
        self.msg.download_state = ""
        self.payload = payload


class TestAudioSupport(unittest.TestCase):

    def setUp(self):
        self.old_db_path = database.DB_PATH
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        database.DB_PATH = self.tmp_db.name
        database.init_db()
        bot.dc_accid = 1
        bot._processed_msg_ids.clear()

    def tearDown(self):
        try:
            os.remove(self.tmp_db.name)
        except Exception:
            pass
        database.DB_PATH = self.old_db_path

    def test_detect_audio_mime_magic_bytes(self):
        # OGG / Opus
        self.assertEqual(bot._detect_audio_mime(b"OggS\x00\x02"), "audio/ogg")
        # MP3 ID3
        self.assertEqual(bot._detect_audio_mime(b"ID3\x03\x00"), "audio/mp3")
        # MP3 sync word
        self.assertEqual(bot._detect_audio_mime(b"\xff\xfb\x90\x44"), "audio/mp3")
        # WAV
        self.assertEqual(bot._detect_audio_mime(b"RIFF\x24\x00\x00\x00WAVEfmt "), "audio/wav")
        # FLAC
        self.assertEqual(bot._detect_audio_mime(b"fLaC\x00\x00\x00\x22"), "audio/flac")
        # AAC ADTS
        self.assertEqual(bot._detect_audio_mime(b"\xff\xf1\x50\x80"), "audio/aac")
        # M4A / MP4
        self.assertEqual(bot._detect_audio_mime(b"\x00\x00\x00\x20ftypM4A \x00"), "audio/mp4")
        # WebM
        self.assertEqual(bot._detect_audio_mime(b"\x1a\x45\xdf\xa3\x93"), "audio/webm")

    def test_detect_audio_mime_extension_fallback(self):
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "voice.opus"), "audio/ogg")
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "voice.ogg"), "audio/ogg")
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "song.mp3"), "audio/mp3")
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "note.m4a"), "audio/mp4")
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "audio.aac"), "audio/aac")
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "sound.wav"), "audio/wav")
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "lossless.flac"), "audio/flac")
        self.assertEqual(bot._detect_audio_mime(b"randomdata", "clip.weba"), "audio/webm")

    def test_detect_audio_mime_declared_mime_normalization(self):
        self.assertEqual(bot._detect_audio_mime(b"", declared_mime="audio/opus; codecs=opus"), "audio/ogg")
        self.assertEqual(bot._detect_audio_mime(b"", declared_mime="audio/mpeg"), "audio/mp3")
        self.assertEqual(bot._detect_audio_mime(b"", declared_mime="audio/x-m4a"), "audio/mp4")
        self.assertEqual(bot._detect_audio_mime(b"", declared_mime="audio/x-wav"), "audio/wav")
        self.assertEqual(bot._detect_audio_mime(b"", declared_mime="audio/x-aac"), "audio/aac")
        self.assertEqual(bot._detect_audio_mime(b"", declared_mime="application/ogg"), "audio/ogg")

    def test_oversized_audio_rejection_declared(self):
        msg = MagicMock()
        msg.file_bytes = 25 * 1024 * 1024  # 25 MB
        with self.assertRaises(bot.MediaTooLargeError):
            bot._download_and_read_media_file(MagicMock(), 1, msg)

    def test_oversized_audio_rejection_on_disk(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.truncate(21 * 1024 * 1024)  # 21 MB
            tmp_path = f.name
        try:
            msg = MagicMock()
            msg.file = tmp_path
            msg.file_bytes = 0
            with self.assertRaises(bot.MediaTooLargeError):
                bot._download_and_read_media_file(MagicMock(), 1, msg)
        finally:
            os.remove(tmp_path)

    def test_extract_audio_from_direct_attachment(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"OggS\x00\x02fake_opus_content")
            tmp_path = f.name
        try:
            msg = MagicMock()
            msg.file = tmp_path
            msg.file_bytes = 0
            msg.view_type = "voice"
            audio_bytes, audio_mime = bot._extract_audio_from_msg_or_quote(MagicMock(), 1, msg)
            self.assertIsNotNone(audio_bytes)
            self.assertEqual(audio_mime, "audio/ogg")
            self.assertTrue(audio_bytes.startswith(b"OggS"))
        finally:
            os.remove(tmp_path)

    def test_extract_audio_from_quoted_message(self):
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
            f.write(b"\x00\x00\x00\x20ftypM4A \x00fake_m4a")
            tmp_path = f.name
        try:
            mock_bot = MagicMock()
            quoted_msg = MagicMock()
            quoted_msg.file = tmp_path
            quoted_msg.file_bytes = 0
            quoted_msg.view_type = "audio"
            mock_bot.rpc.get_message.return_value = quoted_msg

            msg = MagicMock()
            msg.file = None
            msg.file_bytes = 0
            msg.view_type = "text"
            msg.quote = {"message_id": 777}

            audio_bytes, audio_mime = bot._extract_audio_from_msg_or_quote(mock_bot, 1, msg)
            self.assertIsNotNone(audio_bytes)
            self.assertEqual(audio_mime, "audio/mp4")
            mock_bot.rpc.get_message.assert_called_once_with(1, 777)
        finally:
            os.remove(tmp_path)

    @patch.object(bot, "GEMINI_API_KEY", "fake_gemini_key")
    @patch("bot._urlopen")
    def test_call_gemini_api_with_audio(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "Transcription and summary of audio."}]}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        fake_audio = b"OggS\x00\x02mock_audio_data"
        res = bot._call_gemini_api("Transcribe this voice message", media_bytes=fake_audio, media_mime="audio/ogg")
        self.assertEqual(res, "Transcription and summary of audio.")

        req = mock_urlopen.call_args[0][0]
        req_payload = json.loads(req.data.decode("utf-8"))
        parts = req_payload["contents"][0]["parts"]
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["text"], "Transcribe this voice message")
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "audio/ogg")

    @patch.object(bot, "GEMINI_API_KEY", "fake_gemini_key")
    @patch("bot._urlopen")
    def test_summarize_audio_with_gemini_and_caching(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "The speaker discussed the quarterly budget."}]}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        fake_audio = b"OggS\x00\x02mock_voice_note"
        summary1 = bot._summarize_audio_with_gemini(fake_audio, audio_mime="audio/ogg", target_lang="EN", cache_key="voice_test_1")
        self.assertEqual(summary1, "The speaker discussed the quarterly budget.")
        self.assertEqual(mock_urlopen.call_count, 1)

        # Caching: second call should retrieve from db without hitting API
        summary2 = bot._summarize_audio_with_gemini(fake_audio, audio_mime="audio/ogg", target_lang="EN", cache_key="voice_test_1")
        self.assertEqual(summary2, "The speaker discussed the quarterly budget.")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("bot._extract_audio_from_msg_or_quote")
    @patch("bot._react")
    @patch("bot._send")
    def test_handle_tldr_command_oversized_audio(self, mock_send, mock_react, mock_extract):
        mock_extract.side_effect = bot.MediaTooLargeError("File exceeds 20 MB limit.")
        mock_bot = MagicMock()
        event = MockEvent(chat_id=1, msg_id=501, text="/tldr")
        bot._handle_tldr_command(mock_bot, 1, event)

        mock_react.assert_called_once_with(mock_bot, 1, 501, "❌")
        mock_send.assert_called_once()
        self.assertIn("Audio file is too large to summarize (maximum 20 MB)", mock_send.call_args[0][3])

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_tldr_audio")
    def test_handle_tldr_command_with_voice_message(self, mock_do_tldr_audio, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"OggS\x00\x02voice_msg_data")
            tmp_ogg = f.name

        try:
            event = MockEvent(chat_id=1, msg_id=502, text="/tldr", file=tmp_ogg)
            event.msg.file = tmp_ogg
            event.msg.view_type = "voice"

            bot._handle_tldr_command(mock_bot, 1, event)
            mock_do_tldr_audio.assert_called_once()
            args = mock_do_tldr_audio.call_args[0]
            self.assertEqual(args[1], 1)      # accid
            self.assertEqual(args[2], 1)      # chat_id
            self.assertEqual(args[3], 502)    # msg_id
            self.assertTrue(args[5].startswith(b"OggS"))  # audio_bytes
            self.assertEqual(args[6], "audio/ogg")        # audio_mime
        finally:
            os.remove(tmp_ogg)

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_with_voice_message_no_prompt(self, mock_do_ai, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"OggS\x00\x02voice_msg_data")
            tmp_ogg = f.name

        try:
            # User replies /ai to voice message without query
            event = MockEvent(chat_id=1, msg_id=601, text="/ai", file=tmp_ogg)
            event.msg.file = tmp_ogg
            event.msg.view_type = "voice"

            bot._handle_ai_command(mock_bot, 1, event)
            mock_do_ai.assert_called_once()
            args, kwargs = mock_do_ai.call_args
            self.assertEqual(args[5], "")  # empty prompt
            self.assertIsNotNone(kwargs.get("media_bytes"))
            self.assertEqual(kwargs.get("media_type"), "audio")
            self.assertEqual(kwargs.get("media_mime"), "audio/ogg")
        finally:
            os.remove(tmp_ogg)

    @patch("threading.Thread", side_effect=lambda target, args=(), kwargs={}, **kw: MagicMock(start=lambda: target(*args, **kwargs)))
    @patch("bot._is_rate_limited", return_value=False)
    @patch("bot._do_ai_query")
    def test_handle_ai_command_with_voice_message_with_prompt(self, mock_do_ai, mock_rate_limit, mock_thread):
        mock_bot = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
            f.write(b"\x00\x00\x00\x20ftypM4A \x00m4a_data")
            tmp_m4a = f.name

        try:
            # User asks question about voice message
            event = MockEvent(
                chat_id=1,
                msg_id=602,
                payload="What time is the meeting tomorrow?",
                text="/ai What time is the meeting tomorrow?",
                file=tmp_m4a
            )
            event.msg.file = tmp_m4a
            event.msg.view_type = "audio"

            bot._handle_ai_command(mock_bot, 1, event)
            mock_do_ai.assert_called_once()
            args, kwargs = mock_do_ai.call_args
            self.assertEqual(args[5], "What time is the meeting tomorrow?")
            self.assertIsNotNone(kwargs.get("media_bytes"))
            self.assertEqual(kwargs.get("media_type"), "audio")
            self.assertEqual(kwargs.get("media_mime"), "audio/mp4")
        finally:
            os.remove(tmp_m4a)


if __name__ == "__main__":
    unittest.main()
