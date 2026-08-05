import unittest

from pipeline_models import PipelineStage, PipelineStatus, PipelineState


class PipelineModelTests(unittest.TestCase):
    def test_state_accepts_only_declared_forward_transition(self):
        state = PipelineState.new("run-1")
        state = state.transition(PipelineStage.PREFLIGHT, PipelineStatus.RUNNING)
        state = state.transition(PipelineStage.ADAPTER, PipelineStatus.RUNNING)
        self.assertEqual(state.stage, PipelineStage.ADAPTER)

    def test_state_rejects_skipping_from_preflight_to_replica_validation(self):
        state = PipelineState.new("run-1").transition(
            PipelineStage.PREFLIGHT, PipelineStatus.RUNNING
        )
        with self.assertRaisesRegex(ValueError, "invalid pipeline transition"):
            state.transition(PipelineStage.REPLICA_VALIDATION, PipelineStatus.RUNNING)

    def test_terminal_status_cannot_transition(self):
        state = PipelineState.new("run-1").finish(PipelineStatus.FAILED, "preflight")
        with self.assertRaisesRegex(ValueError, "terminal"):
            state.transition(PipelineStage.ADAPTER, PipelineStatus.RUNNING)
