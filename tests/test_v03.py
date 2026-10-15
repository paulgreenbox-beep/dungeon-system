from __future__ import annotations

try:
    from .v03_tests_impl import V03Tests as BaseV03Tests
except ImportError:
    from v03_tests_impl import V03Tests as BaseV03Tests


class V03Tests(BaseV03Tests):
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir._ignore_cleanup_errors = True


del BaseV03Tests
