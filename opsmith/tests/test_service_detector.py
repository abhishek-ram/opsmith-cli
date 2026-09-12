import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from opsmith.core.context import OpsmithContext
from opsmith.core.events import NullSink
from opsmith.service_detector import DockerfileContent, ServiceDetector
from opsmith.tests.conftest import FakeInteraction, FakeProvisionerFactory
from opsmith.types import ServiceInfo, ServiceList, ServiceTypeEnum


def build_context(agent: MagicMock) -> OpsmithContext:
    """
    Builds a context around a mock model, for a detector that never reaches the filesystem.

    :param agent: The mock the detector will call instead of a real model.
    :return: A context with a discarding event sink, a fake user and fake provisioners.
    """
    src_dir = Path("/fake/dir")
    return OpsmithContext(
        src_dir=src_dir,
        deployments_path=src_dir / ".opsmith",
        events=NullSink(),
        interact=FakeInteraction(),
        provisioner_factory=FakeProvisionerFactory(),
        agent=agent,
    )


class TestServiceDetector(unittest.TestCase):
    @patch("opsmith.service_detector.RepoMap")
    def test_detect_services_no_existing_config(self, mock_repo_map):
        """
        Tests that detect_services correctly identifies services when no existing configuration is provided.
        It should call the agent with the correct prompt and process the response to generate service slugs.
        """
        # Arrange
        mock_repo_map_instance = MagicMock()
        mock_repo_map_instance.get_repo_map.return_value = "repo map content"
        mock_repo_map_instance.tracked_files = []
        mock_repo_map.return_value = mock_repo_map_instance

        mock_agent = MagicMock()

        service1 = ServiceInfo(language="python", service_type=ServiceTypeEnum.BACKEND_API)
        service2 = ServiceInfo(
            language="javascript",
            service_type=ServiceTypeEnum.FRONTEND,
            build_cmd="npm run build",
            build_dir="dist",
        )
        service_list = ServiceList(services=[service1, service2])

        mock_run_result = MagicMock()
        mock_run_result.output = service_list
        mock_agent.run_sync.return_value = mock_run_result

        detector = ServiceDetector(ctx=build_context(mock_agent))

        # Act
        result = detector.detect_services()

        # Assert
        self.assertEqual(len(result.services), 2)
        self.assertEqual(result.services[0].name_slug, "python_backend_api_1")
        self.assertEqual(result.services[1].name_slug, "javascript_frontend_1")

        mock_repo_map_instance.get_repo_map.assert_called_once()
        mock_agent.run_sync.assert_called_once()

        call_args = mock_agent.run_sync.call_args
        prompt = call_args.args[0]
        self.assertIn("repo map content", prompt)
        self.assertIn("N/A", prompt)

    @patch("opsmith.service_detector.RepoMap")
    def test_detect_services_with_existing_config(self, mock_repo_map):
        """
        Tests that detect_services correctly uses an existing configuration to provide context to the agent.
        The existing configuration should be part of the prompt.
        """
        # Arrange
        mock_repo_map_instance = MagicMock()
        mock_repo_map_instance.get_repo_map.return_value = "repo map content"
        mock_repo_map_instance.tracked_files = []
        mock_repo_map.return_value = mock_repo_map_instance

        mock_agent = MagicMock()

        existing_service = ServiceInfo(
            name_slug="existing_service",
            language="go",
            service_type=ServiceTypeEnum.BACKEND_WORKER,
        )
        existing_config = ServiceList(services=[existing_service])

        service1 = ServiceInfo(language="python", service_type=ServiceTypeEnum.BACKEND_API)
        service_list = ServiceList(services=[service1])
        mock_run_result = MagicMock()
        mock_run_result.output = service_list
        mock_agent.run_sync.return_value = mock_run_result

        detector = ServiceDetector(ctx=build_context(mock_agent))

        # Act
        result = detector.detect_services(existing_config=existing_config)

        # Assert
        self.assertEqual(len(result.services), 1)
        self.assertEqual(result.services[0].name_slug, "python_backend_api_1")

        mock_agent.run_sync.assert_called_once()

        call_args = mock_agent.run_sync.call_args
        prompt = call_args.args[0]
        self.assertIn("repo map content", prompt)
        self.assertIn("existing_service", prompt)
        self.assertIn("language: go", prompt)

    @patch("opsmith.service_detector.RepoMap")
    def test_detect_services_slug_generation(self, mock_repo_map):
        """
        Tests that name slugs are generated correctly, including handling of multiple services of the same type.
        """
        # Arrange
        mock_repo_map_instance = MagicMock()
        mock_repo_map_instance.get_repo_map.return_value = "repo map content"
        mock_repo_map_instance.tracked_files = []
        mock_repo_map.return_value = mock_repo_map_instance

        mock_agent = MagicMock()

        service1 = ServiceInfo(language="python", service_type=ServiceTypeEnum.BACKEND_API)
        service2 = ServiceInfo(language="python", service_type=ServiceTypeEnum.BACKEND_API)
        service3 = ServiceInfo(
            language="javascript",
            service_type=ServiceTypeEnum.FRONTEND,
            build_cmd="npm run build",
            build_dir="dist",
        )
        service_list = ServiceList(services=[service1, service2, service3])
        mock_run_result = MagicMock()
        mock_run_result.output = service_list
        mock_agent.run_sync.return_value = mock_run_result

        detector = ServiceDetector(ctx=build_context(mock_agent))

        # Act
        result = detector.detect_services()

        # Assert
        self.assertEqual(len(result.services), 3)
        self.assertEqual(result.services[0].name_slug, "python_backend_api_1")
        self.assertEqual(result.services[1].name_slug, "python_backend_api_2")
        self.assertEqual(result.services[2].name_slug, "javascript_frontend_1")


def test_the_dockerfile_editor_is_a_fix_editor(tmp_path):
    """
    When the model gives up, the last attempt is handed to the user to fix by hand and then
    validated again. The editor is declared a fix editor, because accepting a Dockerfile that
    does not build unchanged would only fail the same way later.
    """
    response = MagicMock()
    response.output = DockerfileContent(content="FROM broken\n", give_up=True, reason="stuck")
    response.new_messages.return_value = []
    agent = MagicMock()
    agent.run_sync.return_value = response

    ctx = build_context(agent)
    ctx.interact = FakeInteraction({"dockerfile.edit": "FROM python:3.13\n"})

    with patch("opsmith.service_detector.RepoMap"):
        detector = ServiceDetector(ctx=ctx)

    dockerfile_path = tmp_path / "Dockerfile"
    with patch.object(detector, "_validate_dockerfile", side_effect=[(True, "built", None)]):
        content = detector._generate_and_validate_dockerfile(
            service=ServiceInfo(
                name_slug="api",
                language="python",
                service_type=ServiceTypeEnum.BACKEND_API,
                service_port=8000,
            ),
            dockerfile_path_abs=dockerfile_path,
        )

    assert content == "FROM python:3.13\n"
    assert ctx.interact.asked == [
        {
            "primitive": "edit",
            "key": "dockerfile.edit",
            "message": "Would you like to manually edit the Dockerfile?",
            "path": dockerfile_path,
            "on_headless": "fail",
        }
    ]
