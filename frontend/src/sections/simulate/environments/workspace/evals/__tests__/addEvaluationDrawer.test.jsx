/* eslint-disable react/prop-types */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render as rtlRender,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { serializeEvalConfig } from "src/sections/common/EvalPicker/serializeEvalConfig";
import {
  voiceEvalColumns,
  chatEvalColumns,
} from "src/components/run-tests/common";

const picker = vi.hoisted(() => ({ props: null }));

vi.mock("src/sections/common/EvalPicker", async () => ({
  serializeEvalConfig: (
    await vi.importActual("src/sections/common/EvalPicker/serializeEvalConfig")
  ).serializeEvalConfig,
  EvalPickerDrawer: (props) => {
    picker.props = props;
    if (!props.open) return null;
    return (
      <div data-testid="eval-picker">
        <button
          type="button"
          onClick={() => props.onEvalAdded(PICKED).catch(() => {})}
        >
          save picked
        </button>
        {props.addedEvalAction && (
          <button
            type="button"
            onClick={() => props.addedEvalAction.onClick(props.addedEvals[0])}
          >
            {props.addedEvalAction.label}
          </button>
        )}
        <button type="button" onClick={props.onClose}>
          close picker
        </button>
      </div>
    );
  },
}));
vi.mock("notistack", () => ({ enqueueSnackbar: vi.fn() }));
vi.mock("src/utils/axios", () => ({
  default: { get: vi.fn(), post: vi.fn() },
  endpoints: {
    runTests: {
      detail: (id) => `/simulate/run-tests/${id}/`,
      addEvals: (id) => `/simulate/run-tests/${id}/eval-configs/`,
    },
  },
}));
vi.mock("src/api/simulate-environments/harnessEnvironments", () => ({
  addRunEvaluation: vi.fn(),
  listHarnessEnvironments: vi.fn(),
  deleteHarnessEnvironment: vi.fn(),
  renameHarnessEnvironment: vi.fn(),
  getHarnessEnvironment: vi.fn(),
  deleteAppliedEvaluation: vi.fn(),
}));

const PICKED = {
  templateId: "tpl-tone",
  name: "tone_check",
  model: "turing_large",
  mapping: { output: "call.transcript", input: "" },
  config: {},
  error_localizer_enabled: false,
};
const CONFIGS = [
  {
    id: "c1",
    name: "no_misselling",
    template_id: "tpl-bound",
    mapping: { conversation: "voice_recording" },
  },
  {
    id: "c2",
    name: "call_quality_score",
    template_id: "tpl-result",
    mapping: {},
  },
];

const axios = (await import("src/utils/axios")).default;
const { addRunEvaluation, getHarnessEnvironment } = await import(
  "src/api/simulate-environments/harnessEnvironments"
);
const { enqueueSnackbar } = await import("notistack");
const { default: AddEvaluationDrawer } = await import("../AddEvaluationDrawer");

const render = (ui) =>
  rtlRender(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      {ui}
    </QueryClientProvider>,
  );
const ENV = { id: "env-1" };
const detail = ({ agentType = "voice", runTestId = "rt-1" } = {}) => ({
  overview: {
    agent_type: agentType,
    run: runTestId ? { run_test_id: runTestId } : null,
  },
  evaluations: { selected: [] },
});

beforeEach(() => {
  picker.props = null;
  getHarnessEnvironment.mockReset();
  getHarnessEnvironment.mockResolvedValue(detail());
  axios.get.mockReset();
  axios.get.mockResolvedValue({
    data: { simulate_eval_configs_detail: CONFIGS },
  });
  axios.post.mockReset();
  axios.post.mockResolvedValue({ data: {} });
  addRunEvaluation.mockReset();
  enqueueSnackbar.mockReset();
});

describe("AddEvaluationDrawer — Evaluations tab", () => {
  it("opens the product picker on this environment's run, with everything already on it marked added", async () => {
    render(<AddEvaluationDrawer open env={ENV} onClose={vi.fn()} />);
    await screen.findByTestId("eval-picker");
    await waitFor(() => expect(picker.props.addedEvals).toHaveLength(2));
    expect(picker.props).toMatchObject({
      open: true,
      source: "simulation",
      sourceId: "rt-1",
      sourceColumns: voiceEvalColumns,
      requireInputs: true,
      addedEvals: [
        { id: "tpl-bound", name: "no_misselling", canGrade: true },
        { id: "tpl-result", name: "call_quality_score", canGrade: false },
      ],
      existingEvals: [
        { template_id: "tpl-bound", name: "no_misselling" },
        { template_id: "tpl-result", name: "call_quality_score" },
      ],
    });
    expect(picker.props.addedEvalAction).toBeFalsy();
  });

  it("offers the chat fields to a chat environment", async () => {
    getHarnessEnvironment.mockResolvedValue(detail({ agentType: "chat" }));
    render(<AddEvaluationDrawer open env={ENV} onClose={vi.fn()} />);
    await screen.findByTestId("eval-picker");
    expect(picker.props.sourceColumns).toBe(chatEvalColumns);
  });

  it("adds through the run test's own endpoint with the picker's payload", async () => {
    render(<AddEvaluationDrawer open env={ENV} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByText("save picked"));
    await waitFor(() =>
      expect(axios.post).toHaveBeenCalledWith(
        "/simulate/run-tests/rt-1/eval-configs/",
        {
          evaluations_config: [serializeEvalConfig(PICKED)],
        },
      ),
    );
    await waitFor(() =>
      expect(enqueueSnackbar).toHaveBeenCalledWith("Evaluation added", {
        variant: "success",
      }),
    );
  });

  it("shows a refusal as returned and keeps the picker on its config step", async () => {
    axios.post.mockRejectedValue({
      detail:
        "An evaluation config with the name 'tone_check' already exists in this run test. Please use a different name.",
    });
    render(<AddEvaluationDrawer open env={ENV} onClose={vi.fn()} />);
    await screen.findByTestId("eval-picker");
    await expect(picker.props.onEvalAdded(PICKED)).rejects.toBeTruthy();
    expect(enqueueSnackbar).toHaveBeenCalledWith(
      "An evaluation config with the name 'tone_check' already exists in this run test. Please use a different name.",
      { variant: "error" },
    );
  });

  it("refuses an eval with no inputs mapped before sending anything", async () => {
    render(<AddEvaluationDrawer open env={ENV} onClose={vi.fn()} />);
    await screen.findByTestId("eval-picker");
    await expect(
      picker.props.onEvalAdded({ ...PICKED, mapping: {} }),
    ).rejects.toBeTruthy();
    expect(axios.post).not.toHaveBeenCalled();
    expect(enqueueSnackbar).toHaveBeenCalledWith(
      "This evaluation has no inputs to map, so it can't run in an environment.",
      { variant: "error" },
    );
  });

  it("says so when the environment is not built yet", async () => {
    getHarnessEnvironment.mockResolvedValue(detail({ runTestId: null }));
    render(<AddEvaluationDrawer open env={ENV} onClose={vi.fn()} />);
    expect(await screen.findByText("Not ready yet")).toBeInTheDocument();
    expect(screen.queryByTestId("eval-picker")).toBeNull();
  });

  it("closes through the host", async () => {
    const onClose = vi.fn();
    render(<AddEvaluationDrawer open env={ENV} onClose={onClose} />);
    fireEvent.click(await screen.findByText("close picker"));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("AddEvaluationDrawer — inside a run", () => {
  const COUNTS = {
    queued: 12,
    skipped_existing: 3,
    skipped_in_flight: 0,
    skipped_pending: 1,
    completed_calls: 16,
  };
  const renderRun = () =>
    render(
      <AddEvaluationDrawer
        open
        env={ENV}
        executionId="ex-1"
        onClose={vi.fn()}
      />,
    );

  it("adds the pick, then grades this run with it by name, and reports the counts", async () => {
    addRunEvaluation.mockResolvedValue(COUNTS);
    renderRun();
    fireEvent.click(await screen.findByText("save picked"));
    await waitFor(() =>
      expect(addRunEvaluation).toHaveBeenCalledWith(
        "env-1",
        "ex-1",
        "tone_check",
      ),
    );
    expect(axios.post).toHaveBeenCalledWith(
      "/simulate/run-tests/rt-1/eval-configs/",
      {
        evaluations_config: [serializeEvalConfig(PICKED)],
      },
    );
    await waitFor(() =>
      expect(enqueueSnackbar).toHaveBeenCalledWith(
        expect.stringContaining("Reload this run to see the new verdicts."),
        { variant: "success" },
      ),
    );
  });

  it("keeps the add when grading the run fails, and says which part failed", async () => {
    addRunEvaluation.mockRejectedValue({
      detail: "Run is cancelled; nothing will be graded",
    });
    renderRun();
    await screen.findByTestId("eval-picker");
    await expect(picker.props.onEvalAdded(PICKED)).resolves.toBeUndefined();
    expect(enqueueSnackbar).toHaveBeenCalledWith(
      "Added, but grading this run failed: Run is cancelled; nothing will be graded",
      { variant: "error" },
    );
  });

  it("offers Grade this run only on added evals that have inputs", async () => {
    renderRun();
    await waitFor(() => expect(picker.props?.addedEvals).toHaveLength(2));
    const { show } = picker.props.addedEvalAction;
    expect(picker.props.addedEvals.map(show)).toEqual([true, false]);
  });

  it("grades this run with an eval already on the environment", async () => {
    addRunEvaluation.mockResolvedValue(COUNTS);
    renderRun();
    await waitFor(() => expect(picker.props?.addedEvals).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Grade this run" }));
    await waitFor(() =>
      expect(addRunEvaluation).toHaveBeenCalledWith(
        "env-1",
        "ex-1",
        "no_misselling",
      ),
    );
  });
});
