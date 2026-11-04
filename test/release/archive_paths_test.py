import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'release'))
import archive_paths


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, entries, filename='input.tar.gz'):
        path = self.root / filename
        with tarfile.open(path, 'w:gz') as archive:
            for name, kind, mode in entries:
                member = tarfile.TarInfo(name)
                member.type, member.mode = kind, mode
                member.size = 3 if member.isfile() else 0
                if member.issym(): member.linkname = '/etc'
                archive.addfile(member, io.BytesIO(b'bin') if member.isfile() else None)
        return path

    def test_unsafe_entries_fail_before_any_file_is_extracted(self):
        for name, kind, mode in [('../escape', tarfile.REGTYPE, 0o644),
                ('/', tarfile.DIRTYPE, 0o700), ('////', tarfile.DIRTYPE, 0o700),
                ('/absolute', tarfile.REGTYPE, 0o644), ('bin/../escape', tarfile.REGTYPE, 0o644),
                ('bin/link', tarfile.SYMTYPE, 0o644), ('device', tarfile.CHRTYPE, 0o644),
                ('bin/tool', tarfile.REGTYPE, 0o4755), ('bin/tool', tarfile.REGTYPE, 0o777),
                ('bin\\escape', tarfile.REGTYPE, 0o644), ('bin\nrow', tarfile.REGTYPE, 0o644)]:
            with self.subTest(name=name, kind=kind, mode=mode):
                archive = self.archive([('safe', tarfile.REGTYPE, 0o644), (name, kind, mode)])
                with self.assertRaises(ValueError): archive_paths.extract([archive], self.root / 'install')
                self.assertFalse((self.root / 'install').exists())

    def test_cross_archive_conflicts_and_file_ancestors_fail(self):
        first = self.archive([('bin/tool', tarfile.REGTYPE, 0o755)], 'first.tar.gz')
        for name in ('bin/tool', 'bin', 'bin/tool/subfile'):
            second = self.archive([(name, tarfile.REGTYPE, 0o644)])
            with self.subTest(name=name), self.assertRaises(ValueError):
                archive_paths.extract([first, second], self.root / 'install')

    def test_regular_files_install_with_modes_without_overwriting_existing_tree(self):
        first = self.archive([('./bin/', tarfile.DIRTYPE, 0o755), ('./bin/tool', tarfile.REGTYPE, 0o755)])
        destination = self.root / 'install'
        archive_paths.extract([first], destination)
        self.assertEqual((destination / 'bin/tool').read_bytes(), b'bin')
        self.assertEqual((destination / 'bin/tool').stat().st_mode & 0o7777, 0o755)
        with self.assertRaises(ValueError): archive_paths.extract([first], destination)
        link = self.root / 'link'
        link.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(ValueError): archive_paths.extract([first], link)
        with self.assertRaises(ValueError): archive_paths.extract([first], link / 'nested')
        self.assertFalse((destination / 'nested').exists())

    def test_empty_directories_and_safe_declared_permissions_are_preserved(self):
        archive = self.archive([('private', tarfile.DIRTYPE, 0o700),
                                ('readonly', tarfile.DIRTYPE, 0o500),
                                ('readonly/tool', tarfile.REGTYPE, 0o555)])
        destination = self.root / 'install'
        archive_paths.extract([archive], destination)
        self.assertTrue((destination / 'private').is_dir())
        self.assertEqual((destination / 'private').stat().st_mode & 0o7777, 0o700)
        self.assertEqual((destination / 'readonly').stat().st_mode & 0o7777, 0o500)
        self.assertEqual((destination / 'readonly/tool').read_bytes(), b'bin')
        (destination / 'readonly').chmod(0o700)
