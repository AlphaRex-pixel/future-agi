import { describe, it, expect, vi, beforeEach } from "vitest";
import PropTypes from "prop-types";
import { useEffect } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  MemoryRouter,
  useLocation,
  useNavigate,
  useNavigationType,
} from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const useRunDetail = vi.fn();
const useOptimizationRuns = vi.fn();
const useOptimizerAnalysis = vi.fn();
const useCallExecutionV3Detail = vi.fn();
vi.mock("src/api/simulate-environments/runDetail", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useRunDetail: (...args) => useRunDetail(...args),
    useOptimizationRuns: (...args) => useOptimizationRuns(...args),
    useOptimizerAnalysis: (...args) => useOptimizerAnalysis(...args),
    useCallExecutionV3Detail: (...args) => useCallExecutionV3Detail(...args),
  };
});

// The table's current page, as `?rowId=` reads it to find an on-page row.
const useRunCalls = vi.fn();
vi.mock("src/api/simulate-environments/runCalls", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useRunCalls: (...args) => useRunCalls(...args) };
});

const enqueueSnackbar = vi.fn();
vi.mock("notistack", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    enqueueSnackbar: (...args) => enqueueSnackbar(...args),
  };
});

// The launch drawer hosts the heavy product optimizer form; stub it to a marker
// that can fire the form's onSuccess (which the real drawer maps to onLaunched).
vi.mock(
  "src/sections/test-detail/CreateEditOptimization/CreateEditOptimizationForm",
  () => ({
    default: ({ onSuccess }) => (
      <div>
        optimizer-form
        <button type="button" onClick={() => onSuccess?.()}>
          form-success
        </button>
      </div>
    ),
  }),
);

// The picker owns its own network hooks (including how it grades this run —
// by eval name, not a call count); stub it to a marker that proves the run
// view hands it the execution id (the add goes to the run, not the
// environment). The picker's own behaviour is covered in
// evals/__tests__/addEvaluationDrawer.test.jsx.
function AddEvaluationDrawerStub({ open, executionId }) {
  return open ? <div>add-evals-drawer:{executionId}</div> : null;
}
AddEvaluationDrawerStub.propTypes = {
  open: PropTypes.bool,
  executionId: PropTypes.string,
};
vi.mock("../../../evals/AddEvaluationDrawer", () => ({
  default: AddEvaluationDrawerStub,
}));

// A non-backed environment (client/template — reachable on this route via
// the `?mockRuns=1` QA switch, which mints run history for any env) gets
// the same store-only picker the Evaluations tab falls back to, not the
// real API picker. Stubbed separately so the two are never confused for one
// another.
function AddEvalsDrawerStub({ open, envState }) {
  return open ? (
    <div>add-evals-drawer-fixture:{(envState?.evals || []).length}</div>
  ) : null;
}
AddEvalsDrawerStub.propTypes = {
  open: PropTypes.bool,
  envState: PropTypes.shape({ evals: PropTypes.array }),
};
vi.mock("../../../evals/AddEvalsDrawer", () => ({
  default: AddEvalsDrawerStub,
}));

// The prev/next maths has its own tests; here only the page's wiring matters.
const callListNavigation = vi.fn();
vi.mock("../useCallListNavigation", () => ({
  default: (...args) => callListNavigation(...args),
}));

const TABLE_QUERY = {
  page: 1,
  limit: 50,
  search: "",
  filters: {},
  groupBy: "goal",
};

// The per-call table owns its own network hook, so stub it to a marker that
// reports its query, can open a call, and shows which call/page it follows.
function RunTraceTableStub({
  onOpenCall,
  onQueryChange,
  activeCallId,
  activePage,
}) {
  useEffect(() => {
    onQueryChange?.(TABLE_QUERY);
    return () => onQueryChange?.(null);
  }, [onQueryChange]);
  return (
    <div>
      run-trace-table
      <span>{`active:${activeCallId ?? "-"}:${activePage ?? "-"}`}</span>
      <button
        type="button"
        onClick={() => onOpenCall({ id: "c1", simulationCallType: "voice" })}
      >
        open c1
      </button>
    </div>
  );
}
RunTraceTableStub.propTypes = {
  onOpenCall: PropTypes.func,
  onQueryChange: PropTypes.func,
  activeCallId: PropTypes.string,
  activePage: PropTypes.number,
};
vi.mock("../trace/RunTraceTable", () => ({ default: RunTraceTableStub }));

// Analytics opens a call on its own, with no list behind it.
function RunAnalyticsStub({ onOpenCall }) {
  return (
    <button
      type="button"
      onClick={() => onOpenCall({ id: "c9", simulationCallType: "voice" })}
    >
      open from chart
    </button>
  );
}
RunAnalyticsStub.propTypes = { onOpenCall: PropTypes.func };
vi.mock("../RunAnalytics", () => ({ default: RunAnalyticsStub }));

function CallDrawerStub({ task, hasPrev, hasNext, onPrev, onNext, onClose }) {
  if (!task) return null;
  return (
    <div>
      {`drawer:${task.id}`}
      <span>
        {`task:${task.simulationCallType}|${task.status}|${task.scenario}|${task.persona}`}
      </span>
      <button type="button" onClick={onClose}>
        close call
      </button>
      <button type="button" onClick={onPrev} disabled={!hasPrev}>
        prev call
      </button>
      <button type="button" onClick={onNext} disabled={!hasNext}>
        next call
      </button>
    </div>
  );
}
CallDrawerStub.propTypes = {
  task: PropTypes.object,
  hasPrev: PropTypes.bool,
  hasNext: PropTypes.bool,
  onPrev: PropTypes.func,
  onNext: PropTypes.func,
  onClose: PropTypes.func,
};
vi.mock("../CallDrawer", () => ({ default: CallDrawerStub }));

const { default: RunDetail } = await import("../RunDetail");

const IDENTITY = {
  id: "ex1",
  executionId: "ex1",
  ordinal: 3,
  letter: "3",
  color: "#7857FC",
  name: "Refund Copilot",
  agentVersion: "v2",
  startedAt: "2026-09-10T09:00:00.000Z",
  finishedAt: null,
  status: "passed",
  scenarioIds: ["scenario-a", "scenario-b"],
  trials: 3,
};

const STATS = {
  total: 12,
  passed: 8,
  failed: 4,
  passRate: 74,
  durationS: 600,
  avgDurationMs: 50000,
  scores: {},
  failedCritical: 0,
};

const ENV = { id: "env-1", name: "Refund Copilot", platform: {} };

// A fixture diagnosis view-model (the shape `useOptimizerAnalysis` returns).
const ANALYSIS = {
  status: "completed",
  isWorking: false,
  hasResponse: true,
  summary: "The agent skips the refund-eligibility check.",
  humanComparison: null,
  lastUpdated: "2026-09-15T10:00:00.000Z",
  fixable: [
    {
      id: "agent:0",
      heading: "Confirm eligibility before refunding",
      recommendation: "Add an explicit eligibility gate.",
      breakingPoints: ["Refunded an out-of-window order"],
      priority: "high",
      callExecutionIds: ["c1", "c2"],
      callsAffected: 2,
      branchCategory: "Refunds",
      level: "agent",
    },
  ],
  environmental: [],
};

const OPT_RUN = {
  id: "opt-1",
  name: "Refund fix v1",
  status: "completed",
  optimiserLabel: "ProTeGi",
  trials: 8,
  startedAt: "2026-09-16T09:00:00.000Z",
};

// `backed` defaults to true: every test in this file except the store-only one
// exercises the real (backed) run-detail route, which is what this whole
// suite predates and assumes. `client` lets a test share a spy-wrapped client,
// and any remaining props (e.g. `onStartRun`) pass straight through to RunDetail.
const renderDetail = ({
  backed = true,
  envState = { evals: [] },
  client: passedClient,
  initialEntries = ["/"],
  ...props
} = {}) => {
  const client =
    passedClient ??
    new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>
        <LocationProbe />
        <RunDetail
          env={ENV}
          envState={envState}
          backed={backed}
          testId="rt1"
          executionId="ex1"
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

// Shows the URL the page wrote and whether it replaced the entry.
function LocationProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const navigate = useNavigate();
  return (
    <>
      <div data-testid="location">{`${location.search}|${navigationType}`}</div>
      <button type="button" onClick={() => navigate("/?rowId=nope")}>
        follow link
      </button>
    </>
  );
}

const locationText = () => screen.getByTestId("location").textContent;

const navArgs = () => callListNavigation.mock.calls.at(-1)[0];

beforeEach(() => {
  enqueueSnackbar.mockReset();
  useRunCalls.mockReset();
  useRunCalls.mockReturnValue({ tasks: [], groups: [], isLoading: false });
  useCallExecutionV3Detail.mockReset();
  useCallExecutionV3Detail.mockReturnValue({ data: undefined, error: null });
  callListNavigation.mockReset();
  callListNavigation.mockReturnValue({
    hasPrev: false,
    hasNext: true,
    onPrev: vi.fn(),
    onNext: vi.fn(),
  });
  useRunDetail.mockReturnValue({
    identity: IDENTITY,
    stats: STATS,
    isLoading: false,
  });
  useOptimizationRuns.mockReturnValue({ runs: [], isLoading: false });
  useOptimizerAnalysis.mockReturnValue({
    analysis: ANALYSIS,
    isLoading: false,
    refresh: vi.fn(),
    isRefreshing: false,
  });
});

describe("RunDetail", () => {
  it("renders the identity header with the real ordinal, agent, status and task count", () => {
    useRunDetail.mockReturnValue({
      identity: IDENTITY,
      stats: STATS,
      isLoading: false,
    });
    renderDetail();

    expect(screen.getByText(/Run 3 · agent v2/)).toBeInTheDocument();
    // Some passed, some failed → the run reads "completed", not "Failed".
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText(/Refund Copilot · 12 tasks/)).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /Test runs \(12\)/ }),
    ).toBeInTheDocument();
  });

  it("does not show a Failed verdict while the run is still loading", () => {
    // Loading: identity null and zeroed stats must not read as "Failed".
    useRunDetail.mockReturnValue({
      identity: null,
      stats: { total: 0, passed: 0, failed: 0, passRate: 0, scores: {} },
      isLoading: true,
    });
    renderDetail();
    expect(screen.queryByText("Failed")).toBeNull();
  });

  it("offers Stop simulation in the header only while the run can be stopped", () => {
    useRunDetail.mockReturnValue({
      identity: { ...IDENTITY, status: "running", stoppable: true },
      stats: STATS,
      isLoading: false,
    });
    const { unmount } = renderDetail();
    expect(
      screen.getByRole("button", { name: "Stop simulation" }),
    ).toHaveTextContent("Stop simulation");
    unmount();

    useRunDetail.mockReturnValue({
      identity: IDENTITY,
      stats: STATS,
      isLoading: false,
    });
    renderDetail();
    expect(
      screen.queryByRole("button", { name: "Stop simulation" }),
    ).toBeNull();
  });

  it("shows Cancelling in the header while a stopped run winds down", () => {
    useRunDetail.mockReturnValue({
      identity: { ...IDENTITY, status: "cancelling", stoppable: false },
      stats: STATS,
      isLoading: false,
    });
    renderDetail();

    expect(screen.getByText("Cancelling")).toBeInTheDocument();
    expect(screen.queryByText("Running")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Stop simulation" }),
    ).toBeNull();
  });

  it("shows terminal execution failure despite partial call success", () => {
    useRunDetail.mockReturnValue({
      identity: { ...IDENTITY, status: "failed" },
      stats: STATS,
      isLoading: false,
    });
    renderDetail();

    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("opens the real eval picker from the header action, pointed at this run", async () => {
    useRunDetail.mockReturnValue({
      identity: IDENTITY,
      stats: STATS,
      isLoading: false,
    });
    const user = userEvent.setup();
    renderDetail();

    expect(screen.queryByText(/add-evals-drawer/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add evals" }));
    expect(screen.getByText("add-evals-drawer:ex1")).toBeInTheDocument();
  });

  it("falls back to the store-only picker for a non-backed environment reached via ?mockRuns=1", async () => {
    useRunDetail.mockReturnValue({
      identity: IDENTITY,
      stats: STATS,
      isLoading: false,
    });
    const user = userEvent.setup();
    renderDetail({ backed: false, envState: { evals: ["preset-eval"] } });

    expect(screen.queryByText(/^add-evals-drawer:/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add evals" }));

    // The store-only fixture picker opens, fed the client envState …
    expect(screen.getByText("add-evals-drawer-fixture:1")).toBeInTheDocument();
    // … and the real API picker never mounts — it would 404/error against a
    // client-minted id that has no `/harness-environments/{id}/` backend.
    expect(screen.queryByText(/^add-evals-drawer:/)).toBeNull();
  });

  it("invalidates the optimization runs on launch without a detached-client crash", async () => {
    // refetchOptimizations was a detached invalidateQueries, which throws on
    // this.#queryCache in react-query v5.
    useRunDetail.mockReturnValue({
      identity: IDENTITY,
      stats: STATS,
      isLoading: false,
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();
    renderDetail({ client });

    await user.click(screen.getByRole("button", { name: "Debug failures" }));
    await user.click(
      screen.getByRole("button", { name: "Run Self Improvement" }),
    );
    // The optimizer form's onSuccess flows through the real launch drawer to
    // onLaunched → queryClient.invalidateQueries.
    await user.click(screen.getByRole("button", { name: "form-success" }));

    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["agent-optimization-runs", "ex1"],
    });
  });

  it("submits the same immutable selection and trials on Run again", async () => {
    useRunDetail.mockReturnValue({
      identity: IDENTITY,
      stats: STATS,
      isLoading: false,
    });
    const user = userEvent.setup();
    const onStartRun = vi.fn();
    renderDetail({ onStartRun });

    await user.click(screen.getByRole("button", { name: "Run again" }));

    expect(onStartRun).toHaveBeenCalledWith(["scenario-a", "scenario-b"], 3);
  });

  it("does not invent a critical-failure classification", () => {
    useRunDetail.mockReturnValue({
      identity: IDENTITY,
      stats: STATS,
      isLoading: false,
    });
    renderDetail();

    expect(screen.queryByText(/critical/)).toBeNull();
  });

  it("opens the Debug-failures drawer and renders the real diagnosis", async () => {
    const user = userEvent.setup();
    renderDetail();

    expect(
      screen.queryByText("Confirm eligibility before refunding"),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: "Debug failures" }));

    // The drawer header + the diagnosis summary and the fixture recommendation.
    expect(screen.getByText(/4 failing of 12 measured/)).toBeInTheDocument();
    expect(
      screen.getByText(/skips the refund-eligibility check/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Confirm eligibility before refunding"),
    ).toBeInTheDocument();
    expect(screen.getByText("High priority")).toBeInTheDocument();
  });

  it("launches the first optimization from the diagnosis footer, with no prior runs", async () => {
    useOptimizationRuns.mockReturnValue({ runs: [], isLoading: false });
    const user = userEvent.setup();
    renderDetail();

    // No Trials tab yet (no prior runs), so the footer CTA is the only launch path.
    expect(screen.queryByRole("tab", { name: /Trials/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Debug failures" }));
    const cta = screen.getByRole("button", { name: "Run Self Improvement" });
    expect(cta).toBeInTheDocument();

    await user.click(cta);
    expect(screen.getByText("optimizer-form")).toBeInTheDocument();
  });

  it("shows the Imagine tab as coming soon (no backend)", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(screen.getByRole("button", { name: "Debug failures" }));
    await user.click(screen.getByText("Imagine"));
    expect(screen.getByLabelText("Coming soon")).toBeInTheDocument();
  });

  it("omits the Trials tab when there are no optimization runs", () => {
    useOptimizationRuns.mockReturnValue({ runs: [], isLoading: false });
    renderDetail();

    expect(screen.queryByRole("tab", { name: /Trials/ })).toBeNull();
  });

  it("shows the Trials tab and its runs when optimizations exist", async () => {
    useOptimizationRuns.mockReturnValue({ runs: [OPT_RUN], isLoading: false });
    const user = userEvent.setup();
    renderDetail();

    const trialsTab = screen.getByRole("tab", { name: /Trials \(1\)/ });
    expect(trialsTab).toBeInTheDocument();
    await user.click(trialsTab);
    expect(screen.getByText("Refund fix v1")).toBeInTheDocument();
    expect(screen.getByText(/ProTeGi · 8 trials/)).toBeInTheDocument();
  });

  it("hands the drawer prev/next for a call opened from the table", async () => {
    const user = userEvent.setup();
    const onNext = vi.fn();
    callListNavigation.mockReturnValue({
      hasPrev: false,
      hasNext: true,
      onPrev: vi.fn(),
      onNext,
    });
    renderDetail();

    await user.click(screen.getByRole("button", { name: "open c1" }));
    expect(screen.getByText("drawer:c1")).toBeInTheDocument();
    expect(navArgs()).toMatchObject({
      executionId: "ex1",
      openCall: { task: { id: "c1" }, source: "table", page: null },
      tableQuery: TABLE_QUERY,
      live: false,
    });
    expect(screen.getByRole("button", { name: "prev call" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "next call" }));
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  it("opens the stepped-to call and has the table follow to its page", async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(screen.getByRole("button", { name: "open c1" }));

    act(() =>
      navArgs().onStep({
        task: { id: "c51", simulationCallType: "voice" },
        source: "table",
        page: 2,
      }),
    );
    expect(screen.getByText("drawer:c51")).toBeInTheDocument();
    expect(screen.getByText("active:c51:2")).toBeInTheDocument();
  });

  it("tells the navigation a live run is live", async () => {
    const user = userEvent.setup();
    useRunDetail.mockReturnValue({
      identity: { ...IDENTITY, status: "running", stoppable: true },
      stats: STATS,
      isLoading: false,
    });
    renderDetail();
    await user.click(screen.getByRole("button", { name: "open c1" }));
    expect(navArgs().live).toBe(true);
  });

  it("marks a call opened from Analytics as not from the table", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(screen.getByRole("tab", { name: "Analytics" }));
    await user.click(screen.getByRole("button", { name: "open from chart" }));

    expect(screen.getByText("drawer:c9")).toBeInTheDocument();
    expect(navArgs().openCall).toMatchObject({ source: "analytics" });
    // The table unmounted with the tab switch, so it no longer reports a query.
    expect(navArgs().tableQuery).toBeNull();
  });
});

// A trimmed v3 call-detail payload for a chat call that isn't on the table's
// current page.
const CHAT_DETAIL = {
  id: "x1",
  status: "completed",
  scenario: "Refund request",
  persona: "Impatient Ian",
  outcome: "failed",
  simulation_call_type: "text",
  evaluations: [],
  eval_metrics: {},
};

// Answers the detail fetch only once RunDetail actually enables it.
const detailFor = (id, result) =>
  useCallExecutionV3Detail.mockImplementation((callId, enabled) =>
    callId === id && enabled ? result : { data: undefined, error: null },
  );

describe("RunDetail ?rowId=", () => {
  it("opens a voice row on the table's current page from the URL, with prev/next", async () => {
    // Like react-query: a disabled page query holds no rows and reads pending.
    useRunCalls.mockImplementation((_, { enabled }) =>
      enabled
        ? {
            tasks: [
              { id: "c6", simulationCallType: "voice" },
              { id: "c7", simulationCallType: "voice", status: "passed", scenario: "Billing", persona: "Pat" },
            ],
            groups: [],
            isLoading: false,
          }
        : { tasks: [], groups: [], isLoading: true },
    );
    renderDetail({ initialEntries: ["/?foo=1&rowId=c7"] });

    expect(await screen.findByText("drawer:c7")).toBeInTheDocument();
    expect(screen.getByText("task:voice|passed|Billing|Pat")).toBeInTheDocument();
    expect(navArgs().openCall).toMatchObject({ task: { id: "c7" }, source: "table" });
    expect(useRunCalls).toHaveBeenCalledWith(
      "ex1",
      expect.objectContaining({ ...TABLE_QUERY, enabled: true }),
    );
    // The row came off the table page; the detail was never needed.
    expect(useCallExecutionV3Detail.mock.calls.some(([, enabled]) => enabled)).toBe(false);
    expect(locationText()).toContain("foo=1");
    expect(locationText()).toContain("rowId=c7");
  });

  it("opens an off-page chat row from its detail, with its fields and no prev/next", async () => {
    detailFor("x1", { data: CHAT_DETAIL, error: null });
    renderDetail({ initialEntries: ["/?rowId=x1"] });

    expect(await screen.findByText("drawer:x1")).toBeInTheDocument();
    expect(
      screen.getByText("task:text|failed|Refund request|Impatient Ian"),
    ).toBeInTheDocument();
    expect(navArgs().openCall.source).not.toBe("table");
    expect(locationText()).toContain("rowId=x1");
  });

  it("opens an off-page voice row from its detail", async () => {
    detailFor("v1", {
      data: { id: "v1", simulation_call_type: "voice", outcome: "passed", scenario: "Billing" },
      error: null,
    });
    renderDetail({ initialEntries: ["/?rowId=v1"] });

    expect(await screen.findByText("drawer:v1")).toBeInTheDocument();
    expect(screen.getByText(/^task:voice\|passed\|Billing/)).toBeInTheDocument();
  });

  it("shows no drawer while the table page is still loading", () => {
    useRunCalls.mockReturnValue({ tasks: [], groups: [], isLoading: true });
    detailFor("x1", { data: CHAT_DETAIL, error: null });
    renderDetail({ initialEntries: ["/?rowId=x1"] });

    expect(screen.queryByText(/^drawer:/)).toBeNull();
    expect(locationText()).toContain("rowId=x1");
  });

  it("fetches an off-page row once the table page settles without it", async () => {
    let pageLoading = true;
    useRunCalls.mockImplementation(() => ({ tasks: [], groups: [], isLoading: pageLoading }));
    detailFor("x1", { data: CHAT_DETAIL, error: null });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = renderDetail({ client, initialEntries: ["/?rowId=x1"] });

    expect(screen.queryByText(/^drawer:/)).toBeNull();
    expect(useCallExecutionV3Detail.mock.calls.some(([, enabled]) => enabled)).toBe(false);

    pageLoading = false;
    rerender(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/?rowId=x1"]}>
          <LocationProbe />
          <RunDetail env={ENV} envState={{ evals: [] }} backed testId="rt1" executionId="ex1" />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("drawer:x1")).toBeInTheDocument();
    expect(navArgs().openCall.source).not.toBe("table");
  });

  it("reports an unknown row and drops the param", async () => {
    detailFor("nope", { data: undefined, error: { statusCode: 404 } });
    renderDetail({ initialEntries: ["/?foo=1&rowId=nope"] });

    await waitFor(() => expect(locationText()).not.toContain("rowId"));
    expect(locationText()).toContain("foo=1");
    expect(enqueueSnackbar).toHaveBeenCalledWith("Row not found", { variant: "error" });
    expect(screen.queryByText(/^drawer:/)).toBeNull();
  });

  it("reports the same unknown row again when its link is followed a second time", async () => {
    const user = userEvent.setup();
    detailFor("nope", { data: undefined, error: { statusCode: 404 } });
    renderDetail({ initialEntries: ["/?rowId=nope"] });
    await waitFor(() => expect(locationText()).not.toContain("rowId"));

    await user.click(screen.getByRole("button", { name: "follow link" }));

    await waitFor(() => expect(enqueueSnackbar).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(locationText()).not.toContain("rowId"));
  });

  it("says the row couldn't load when the detail fails for another reason", async () => {
    detailFor("x9", { data: undefined, error: { statusCode: 500 } });
    renderDetail({ initialEntries: ["/?rowId=x9"] });

    await waitFor(() => expect(locationText()).not.toContain("rowId"));
    expect(enqueueSnackbar).toHaveBeenCalledWith("Couldn't load the row", { variant: "error" });
    expect(screen.queryByText(/^drawer:/)).toBeNull();
  });

  it("writes the open row to the URL on open, step and close, replacing the entry", async () => {
    const user = userEvent.setup();
    renderDetail({ initialEntries: ["/?foo=1"] });

    await user.click(screen.getByRole("button", { name: "open c1" }));
    expect(locationText()).toBe("?foo=1&rowId=c1|REPLACE");

    act(() =>
      navArgs().onStep({
        task: { id: "c51", simulationCallType: "voice" },
        source: "table",
        page: 2,
      }),
    );
    expect(screen.getByText("drawer:c51")).toBeInTheDocument();
    expect(locationText()).toBe("?foo=1&rowId=c51|REPLACE");

    await user.click(screen.getByRole("button", { name: "close call" }));
    expect(screen.queryByText(/^drawer:/)).toBeNull();
    expect(locationText()).toBe("?foo=1|REPLACE");
  });

  it("writes a row opened from Analytics to the URL", async () => {
    const user = userEvent.setup();
    renderDetail();

    await user.click(screen.getByRole("tab", { name: "Analytics" }));
    await user.click(screen.getByRole("button", { name: "open from chart" }));

    expect(locationText()).toBe("?rowId=c9|REPLACE");
  });
});
