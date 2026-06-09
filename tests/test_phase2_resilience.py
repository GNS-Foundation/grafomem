import pytest
import time
from uuid import uuid4
from unittest.mock import patch, MagicMock

from aml.cloud.orchestrator import OrchestratorService, WorkflowMode, StepStatus
from aml.cloud.llm_registry import LLMRegistry, LLMProvider, LLMResponse, LLMRequest, LLMConfig
from aml.cloud.governance import GovernanceGateway, EvaluationLog, EvaluationResult

@pytest.fixture
def test_tenant_id():
    return f"tenant_{uuid4().hex[:8]}"

@pytest.fixture
def db_conn_mock():
    conn = MagicMock()
    conn.execute.return_value = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    conn.execute.return_value.fetchall.return_value = []
    
    # Mock rowcount for delete
    conn.execute.return_value.rowcount = 1
    return conn

@pytest.fixture
def mock_db(monkeypatch, db_conn_mock):
    def fake_connect(*args, **kwargs):
        return db_conn_mock
    import psycopg
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    return db_conn_mock

@pytest.fixture
def registry(mock_db):
    class DummyEncryption:
        def encrypt(self, p): return p
        def decrypt(self, c): return c

    reg = LLMRegistry("postgresql://fake", encryption=DummyEncryption())
    reg._conn = mock_db
    yield reg
    reg.close()

@pytest.fixture
def governance(mock_db):
    gov = GovernanceGateway("postgresql://fake")
    gov._conn = mock_db
    yield gov
    gov.close()

@pytest.fixture
def orchestrator(mock_db, registry, governance):
    orch = OrchestratorService(
        db_url="postgresql://fake",
        llm_registry=registry,
        governance=governance,
        decision_trail=MagicMock(),
    )
    orch._conn = mock_db
    # Mocking id generators and time to have predictable workflow creation
    with patch("aml.cloud.orchestrator._compute_id", return_value="fake_id"):
        yield orch
    orch.close()

def test_llm_provider_failover(orchestrator, registry, test_tenant_id):
    """Test that a failing primary provider falls back correctly and logs the failure."""
    # Register models
    registry.register_provider(test_tenant_id, LLMProvider.MOCK, "mock-primary")
    registry.register_provider(test_tenant_id, LLMProvider.MOCK, "mock-fallback")

    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-primary"
    agent_def.fallback_models = ["mock-fallback"]
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = []
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "FailoverAgent"

    def failing_infer(tenant_id: str, req: LLMRequest) -> LLMResponse:
        if req.model_id == "mock-primary":
            raise RuntimeError("Injected Transient 503 Error")
        return LLMResponse(
            content="[mock response]",
            tool_calls=[],
            tokens_input=10,
            tokens_output=10,
            model_id=req.model_id,
            latency_ms=10,
            raw_response={},
        )

    with patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1), \
         patch.object(orchestrator, 'get_workflow_steps', return_value=[]), \
         patch.object(registry, 'infer', side_effect=failing_infer):
        
        step = orchestrator.execute_step(
            workflow_id="wf_id",
            agent_id="agent_id",
            input_text="Hello?",
        )

    # Output should reflect success from fallback
    assert "[mock response]" in step.raw_output or "mock-fallback" in step.model_id
    assert step.model_id == "mock-fallback"


def test_llm_provider_no_failover_success(orchestrator, registry, test_tenant_id):
    """Test that a successful primary provider call does not trigger failover."""
    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-primary"
    agent_def.fallback_models = ["mock-fallback"]
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = []
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "FailoverAgent"

    def successful_infer(tenant_id: str, req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="[primary response]",
            tool_calls=[],
            tokens_input=10,
            tokens_output=10,
            model_id=req.model_id,
            latency_ms=10,
            raw_response={},
        )

    with patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1), \
         patch.object(orchestrator, 'get_workflow_steps', return_value=[]), \
         patch.object(registry, 'infer', side_effect=successful_infer):
        
        step = orchestrator.execute_step(
            workflow_id="wf_id",
            agent_id="agent_id",
            input_text="Hello?",
        )

    assert "[primary response]" in step.raw_output
    assert step.model_id == "mock-primary"


def test_tool_governance_denial(orchestrator, governance, test_tenant_id):
    """Test that pre-flight tool governance correctly blocks unauthorized tools."""
    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-primary"
    agent_def.fallback_models = []
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = ["dangerous_tool"]
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "ToolAgent"

    orchestrator._tool_registry = MagicMock()
    orchestrator._tool_registry.execute.return_value = MagicMock(output="Tool executed", success=True, governance_allowed=True)

    with patch.object(orchestrator._llm_registry, 'infer') as mock_infer, \
         patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1), \
         patch.object(orchestrator, 'get_workflow_steps', return_value=[]):
        mock_infer.return_value = LLMResponse(
            content="",
            tool_calls=[{"name": "dangerous_tool", "arguments": {"x": 1}}],
            tokens_input=10,
            tokens_output=10,
            model_id="mock-primary",
            latency_ms=10,
            raw_response={},
        )
        
        # Override governance to deny tool execution
        original_evaluate = governance.evaluate_and_gate
        def deny_tool_execution(tenant_id, category, context):
            if category == "tool_execution":
                return False, [EvaluationLog(
                    log_id="log1",
                    tenant_id=tenant_id,
                    policy_id="test",
                    policy_name="test",
                    result=EvaluationResult.DENIED,
                    operation=category,
                    detail="Denied",
                    request_summary="",
                )]
            return original_evaluate(tenant_id, category, context)
            
        with patch.object(governance, 'evaluate_and_gate', side_effect=deny_tool_execution):
            step = orchestrator.execute_step(
                workflow_id="wf_id",
                agent_id="agent_id",
                input_text="Run tool"
            )

    assert len(step.tool_results) == 1
    assert step.tool_results[0]["success"] is False
    assert step.tool_results[0]["governance_allowed"] is False
    assert "denied" in step.tool_results[0]["output"].lower()


def test_tool_governance_allow(orchestrator, governance, test_tenant_id):
    """Test that pre-flight tool governance correctly executes authorized tools."""
    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-primary"
    agent_def.fallback_models = []
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = ["safe_tool"]
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "ToolAgent"

    orchestrator._tool_registry = MagicMock()
    orchestrator._tool_registry.execute.return_value = MagicMock(output="Tool safely executed", success=True, governance_allowed=True)

    with patch.object(orchestrator._llm_registry, 'infer') as mock_infer, \
         patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1), \
         patch.object(orchestrator, 'get_workflow_steps', return_value=[]):
        mock_infer.return_value = LLMResponse(
            content="",
            tool_calls=[{"name": "safe_tool", "arguments": {"x": 1}}],
            tokens_input=10,
            tokens_output=10,
            model_id="mock-primary",
            latency_ms=10,
            raw_response={},
        )
        
        # Override governance to allow tool execution
        original_evaluate = governance.evaluate_and_gate
        def allow_tool_execution(tenant_id, category, context):
            if category == "tool_execution":
                return True, [EvaluationLog(
                    log_id="log2",
                    tenant_id=tenant_id,
                    policy_id="test",
                    policy_name="test",
                    result=EvaluationResult.ALLOWED,
                    operation=category,
                    detail="Allowed",
                    request_summary="",
                )]
            return original_evaluate(tenant_id, category, context)
            
        with patch.object(governance, 'evaluate_and_gate', side_effect=allow_tool_execution):
            step = orchestrator.execute_step(
                workflow_id="wf_id",
                agent_id="agent_id",
                input_text="Run safe tool"
            )

    assert len(step.tool_results) == 1
    assert step.tool_results[0]["success"] is True
    assert step.tool_results[0]["governance_allowed"] is True
    assert "Tool safely executed" in step.tool_results[0]["output"]


def test_exact_repeat_detection(orchestrator, registry, test_tenant_id):
    """Test hash-based exact-repeat detection catches loops within last 4 steps."""
    registry.register_provider(test_tenant_id, LLMProvider.MOCK, "mock-repeat")
    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-repeat"
    agent_def.fallback_models = []
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = []
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "RepeatAgent"

    with patch.object(orchestrator._llm_registry, 'infer') as mock_infer, \
         patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1):
        
        mock_infer.return_value = LLMResponse(
            content="I am stuck in a loop.",
            tool_calls=[],
            tokens_input=10,
            tokens_output=10,
            model_id="mock-repeat",
            latency_ms=10,
            raw_response={},
        )
        
        step1_mock = MagicMock()
        step1_mock.agent_id = "agent_id"
        step1_mock.raw_output = "I am stuck in a loop."
        step1_mock.tool_calls = []

        # A completely different step in between shouldn't break the detector if it's within N=4
        step2_mock = MagicMock()
        step2_mock.agent_id = "agent_id"
        step2_mock.raw_output = "Something else entirely."
        step2_mock.tool_calls = []

        with patch.object(orchestrator, 'get_workflow_steps', return_value=[step1_mock, step2_mock]):
            step3 = orchestrator.execute_step("wf_id", "agent_id", "Hello again")
            assert step3.status == StepStatus.HALTED_LOOP
            assert step3.raw_output == "[Error: Exact-Repeat Detected]"


def test_near_repeat_not_halted(orchestrator, registry, test_tenant_id):
    """Test that slightly different outputs are NOT halted by repeat detection."""
    registry.register_provider(test_tenant_id, LLMProvider.MOCK, "mock-repeat")
    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-repeat"
    agent_def.fallback_models = []
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = []
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "RepeatAgent"

    with patch.object(orchestrator._llm_registry, 'infer') as mock_infer, \
         patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1):
        
        mock_infer.return_value = LLMResponse(
            content="I am stuck in a loop. Wait, I changed a character!",
            tool_calls=[],
            tokens_input=10,
            tokens_output=10,
            model_id="mock-repeat",
            latency_ms=10,
            raw_response={},
        )
        
        step1_mock = MagicMock()
        step1_mock.agent_id = "agent_id"
        step1_mock.raw_output = "I am stuck in a loop."
        step1_mock.tool_calls = []

        with patch.object(orchestrator, 'get_workflow_steps', return_value=[step1_mock]):
            step2 = orchestrator.execute_step("wf_id", "agent_id", "Hello again")
            assert step2.status != StepStatus.HALTED_LOOP
            assert step2.raw_output != "[Error: Exact-Repeat Detected]"
            assert "changed a character" in step2.raw_output

def test_workflow_timeout_positive(orchestrator, registry, test_tenant_id):
    """Test that a workflow well within the deadline executes normally."""
    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-primary"
    agent_def.fallback_models = []
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = []
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "TimeoutPositiveAgent"

    def successful_infer(tenant_id: str, req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="[executed in time]",
            tool_calls=[],
            tokens_input=10,
            tokens_output=10,
            model_id=req.model_id,
            latency_ms=10,
            raw_response={},
        )

    with patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1), \
         patch.object(orchestrator, 'get_workflow_steps', return_value=[]), \
         patch.object(registry, 'infer', side_effect=successful_infer):
        
        # deadline is 10 seconds in the future
        deadline = time.monotonic() + 10.0
        
        step = orchestrator.execute_step(
            workflow_id="wf_id",
            agent_id="agent_id",
            input_text="Hello?",
            deadline=deadline
        )

    assert "[executed in time]" in step.raw_output
    assert step.status == StepStatus.COMPLETED

def test_workflow_timeout_negative(orchestrator, registry, test_tenant_id):
    """Test that a workflow exceeding its deadline is halted."""
    agent_def = MagicMock()
    agent_def.agent_id = "agent_id"
    agent_def.model_id = "mock-primary"
    agent_def.fallback_models = []
    agent_def.system_prompt = "You are a test agent."
    agent_def.tenant_id = test_tenant_id
    agent_def.memory_stores = []
    agent_def.tools = []
    agent_def.temperature = 0.5
    agent_def.max_tokens_per_step = 100
    agent_def.name = "TimeoutNegativeAgent"

    def slow_infer(tenant_id: str, req: LLMRequest) -> LLMResponse:
        # Simulate LLM taking too long
        time.sleep(0.1)
        return LLMResponse(
            content="[too slow]",
            tool_calls=[],
            tokens_input=10,
            tokens_output=10,
            model_id=req.model_id,
            latency_ms=10,
            raw_response={},
        )

    with patch.object(orchestrator, 'get_agent', return_value=agent_def), \
         patch.object(orchestrator, '_next_step_number', return_value=1), \
         patch.object(orchestrator, 'get_workflow_steps', return_value=[]), \
         patch.object(registry, 'infer', side_effect=slow_infer):
        
        # deadline is in the past!
        deadline = time.monotonic() - 10.0
        
        step = orchestrator.execute_step(
            workflow_id="wf_id",
            agent_id="agent_id",
            input_text="Hello?",
            deadline=deadline
        )

    assert step.status == StepStatus.FAILED_TIMEOUT
