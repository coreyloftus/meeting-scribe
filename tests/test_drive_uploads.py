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

    @patch.object(google_auth, 'upload_file', return_value=('id', 'url'))
    def test_summary_preserves_metadata_without_empty_notes(self, upload):
        self.note.warnings = ['Silent microphone']
        self.note.audio_path = 'recording.flac'
        self.note.user_notes = '   \n'
        gdrive.write(self.cfg(transcript=False), self.note)
        content = upload.call_args.args[2]
        for value in ('Silent microphone', 'recording.flac', '2026-11-10'):
            self.assertIn(value, content)
        self.assertNotIn('My Notes', content)
        self.assertNotIn('Full Transcript', content)

    @patch.object(google_auth, 'upload_file', return_value=('id', 'url'))
    def test_null_folder_option_uses_saved_folder(self, upload):
        gdrive.write(self.cfg(folder_id='saved', transcript=False), self.note, {'folder_id': None})
        self.assertEqual(upload.call_args.kwargs['folder_id'], 'saved')

    @patch.object(google_auth, 'session')
    def test_invalid_upload_host_rejected(self, session):
        path = Mock(spec=Path)
        path.stat.return_value.st_size = 42
        session.return_value.post.return_value.headers = {'Location': 'https://example.com/upload'}
        with self.assertRaises(google_auth.GoogleAuthError):
            google_auth.upload_recording(self.cfg(), path, 'audio.flac', None)
        session.return_value.put.assert_not_called()

    @patch.object(google_auth, '_save_token')
    @patch.object(google_auth, '_imports')
    @patch.object(google_auth, 'session')
    def test_unauthorized_upload_reopens_stream(self, session, imports, save):
        import io
        imports.return_value = (None, Mock(), None, None)
        path = Mock(spec=Path)
        path.stat.return_value.st_size = 1000
        path.open.side_effect = lambda mode: io.BytesIO(b'x' * 1000)
        session.return_value.post.return_value.headers = {'Location': 'https://www.googleapis.com/upload/session'}
        sizes = []
        def put(url, data, **kwargs):
            sizes.append(len(data.read()))
            response = Mock(status_code=401 if len(sizes) == 1 else 200)
            response.json.return_value = {'id': 'audio'}
            return response
        session.return_value.put.side_effect = put
        google_auth.upload_recording(self.cfg(), path, 'audio.flac', None)
        self.assertEqual(sizes, [1000, 1000])
        session.assert_called_once_with(unittest.mock.ANY, max_refresh_attempts=0)

    def test_drive_is_queued_after_saved_summary_and_compression(self):
        from meeting_scribe.daemon import server
        from meeting_scribe.daemon.jobs import Job
        events = []
        meeting = {'id': 'test', 'system_wav': 'test.system.wav', 'base_path': 'test'}
        result = Mock(note=self.note, warnings=[], transcript_path=None, outputs=[])
        cfg = Mock(compress_audio=True)
        cfg.enabled_outputs.return_value = ['gdrive']
        with patch.object(server, 'DB') as db, patch.object(server, 'cfg', return_value=cfg), \
             patch.object(server, 'BUS'), patch.object(server, '_previous_urls', return_value={}), \
             patch.object(server.process_mod, 'process', return_value=result) as process, \
             patch.object(server, '_compress_audio', side_effect=lambda *a: events.append('compress')), \
             patch.object(server.JOBS, 'submit', side_effect=lambda *a: events.append('push')):
            db.get_meeting.return_value = meeting
            db.update_meeting.side_effect = lambda *a, **kw: events.append(kw.get('status'))
            server._handle_process(Job(1, 'process', 'test'))
        self.assertEqual(events, ['done', 'compress', 'push'])
        self.assertTrue(process.call_args.kwargs['defer_drive'])


if __name__ == '__main__':
    unittest.main()
