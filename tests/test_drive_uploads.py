import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from meeting_scribe.config import Config
from meeting_scribe.outputs import Note
from meeting_scribe.outputs import gdrive
from meeting_scribe.integrations import google_auth


class DriveUploads(unittest.TestCase):
    def setUp(self):
        self.note = Note('Meeting', '2026-11-10', 'meeting', 'Summary', 'Transcript')

    def cfg(self, **choices):
        return Config({'outputs': {'gdrive': choices}}, None)

    @patch.object(google_auth, 'upload_file', return_value=('id', 'https://drive/file'))
    def test_summary_only_excludes_transcript_and_audio(self, upload):
        result = gdrive.write(self.cfg(summary=True, transcript=False, recording=False), self.note)
        self.assertTrue(result.ok)
        self.assertEqual(upload.call_count, 1)
        self.assertNotIn('Transcript', upload.call_args.args[2])

    @patch.object(google_auth, 'upload_file', side_effect=[RuntimeError('failed summary'), ('id', 'url')])
    def test_partial_failure_reports_success_and_failure(self, upload):
        result = gdrive.write(self.cfg(), self.note)
        self.assertFalse(result.ok)
        self.assertEqual(upload.call_count, 2)
        self.assertIn('Uploaded 1', result.detail)
        self.assertIn('failed summary', result.detail)

    @patch.object(google_auth, 'upload_file')
    def test_no_selection_does_not_upload(self, upload):
        result = gdrive.write(self.cfg(summary=False, transcript=False), self.note)
        self.assertFalse(result.ok)
        upload.assert_not_called()

    def test_missing_audio_is_reported(self):
        result = gdrive.write(self.cfg(summary=False, transcript=False, recording=True), self.note)
        self.assertFalse(result.ok)
        self.assertIn('No recording', result.detail)

    @patch.object(google_auth, 'session')
    def test_existing_folder_is_reused(self, session):
        session.return_value.get.return_value.json.return_value = {'files': [{'id': 'existing'}]}
        self.assertEqual(google_auth.meeting_folder(self.cfg()), 'existing')
        session.return_value.post.assert_not_called()

    @patch.object(google_auth, 'session')
    def test_missing_folder_is_created(self, session):
        session.return_value.get.return_value.json.return_value = {'files': []}
        session.return_value.post.return_value.json.return_value = {'id': 'created'}
        self.assertEqual(google_auth.meeting_folder(self.cfg()), 'created')
        self.assertEqual(session.return_value.post.call_args.kwargs['json']['name'], 'Meeting Scribe')

    @patch.object(google_auth, 'session')
    def test_audio_stream_and_parent(self, session):
        path = Mock(spec=Path)
        path.stat.return_value.st_size = 42
        stream = Mock()
        path.open.return_value = MagicMock()
        path.open.return_value.__enter__.return_value = stream
        session.return_value.post.return_value.headers = {'Location': 'https://www.googleapis.com/upload/session'}
        session.return_value.put.return_value.json.return_value = {'id': 'audio'}
        google_auth.upload_recording(self.cfg(), path, 'recording.flac', 'folder')
        self.assertEqual(session.return_value.post.call_args.kwargs['json']['parents'], ['folder'])
        self.assertIs(session.return_value.put.call_args.kwargs['data'], stream)


if __name__ == '__main__':
    unittest.main()
