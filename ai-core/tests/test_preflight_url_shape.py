"""사용자가 지정하지 않은 대상을 검증하지 않는다.

`_normalize_github_url`이 `len(parts) < 2`를 썼다. owner/repo 뒤에 뭐가 붙어 있어도 앞의
두 조각만 떼어 레포 루트로 바꿨다. 그래서 PR 링크나 파일 링크를 붙여넣으면 조용히 레포
전체를 검증하고 그 결과를 돌려줬다. 사용자가 물은 것과 다른 것을 답한 셈이다.
"""

import unittest

from app.repository.preflight import _normalize_github_url


class AcceptedShapeTests(unittest.TestCase):
    def test_the_repository_root_is_accepted(self) -> None:
        self.assertEqual(
            _normalize_github_url("https://github.com/benjaminp/six"),
            "https://github.com/benjaminp/six.git",
        )

    def test_a_dot_git_suffix_is_accepted(self) -> None:
        self.assertEqual(
            _normalize_github_url("https://github.com/benjaminp/six.git"),
            "https://github.com/benjaminp/six.git",
        )

    def test_a_trailing_slash_is_accepted(self) -> None:
        self.assertEqual(
            _normalize_github_url("https://github.com/benjaminp/six/"),
            "https://github.com/benjaminp/six.git",
        )


class RejectedShapeTests(unittest.TestCase):
    """레포 루트가 아닌 링크는 거절한다. 잘라서 루트로 바꾸지 않는다."""

    def test_a_pull_request_link_is_rejected(self) -> None:
        self.assertIsNone(_normalize_github_url("https://github.com/benjaminp/six/pull/5"))

    def test_a_file_link_is_rejected(self) -> None:
        self.assertIsNone(
            _normalize_github_url("https://github.com/benjaminp/six/blob/main/six.py")
        )

    def test_a_tree_link_is_rejected(self) -> None:
        self.assertIsNone(_normalize_github_url("https://github.com/benjaminp/six/tree/main"))

    def test_an_owner_without_a_repository_is_rejected(self) -> None:
        self.assertIsNone(_normalize_github_url("https://github.com/benjaminp"))

    def test_a_non_github_host_is_rejected(self) -> None:
        self.assertIsNone(_normalize_github_url("https://gitlab.com/benjaminp/six"))

    def test_http_is_rejected(self) -> None:
        self.assertIsNone(_normalize_github_url("http://github.com/benjaminp/six"))


if __name__ == "__main__":
    unittest.main()
