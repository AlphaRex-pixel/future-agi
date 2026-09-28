import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { ThemeProvider, createTheme } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { palette } from "src/theme/palette";
import EvalPickerDrawer from "../EvalPickerDrawer";

const data = vi.hoisted(() => ({ searchQuery: "" }));
const ITEMS = [
  {
    id: "tpl-a",
    name: "Alpha eval",
    template_type: "single",
    eval_type: "llm",
    output_type: "pass_fail",
    created_by_name: "System",
  },
  {
    id: "tpl-b",
    name: "Beta eval",
    template_type: "single",
    eval_type: "llm",
    output_type: "pass_fail",
    created_by_name: "System",
  },
  {
    id: "tpl-c",
    name: "Gamma eval",
    template_type: "single",
    eval_type: "code",
    output_type: "pass_fail",
    created_by_name: "System",
  },
];

vi.mock("../hooks/useEvalPickerData", () => ({
  useEvalPickerData: () => ({
    items: ITEMS,
    total: ITEMS.length,
    isLoading: false,
    isSearching: false,
    searchQuery: data.searchQuery,
    setSearchQuery: vi.fn(),
    page: 0,
    setPage: vi.fn(),
    pageSize: 25,
    setPageSize: vi.fn(),
    sorting: [],
    setSorting: vi.fn(),
    filters: null,
    setFilters: vi.fn(),
  }),
}));
vi.mock("../EvalPickerConfigFull", () => ({
  default: () => <div data-testid="config-stub" />,
}));

const theme = createTheme({
  palette: palette("light"),
  spacing: (f) => `${0.25 * f}rem`,
});
const renderDrawer = (props) =>
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <BrowserRouter>
        <ThemeProvider theme={theme}>
          <EvalPickerDrawer
            open
            onClose={vi.fn()}
            source="simulation"
            {...props}
          />
        </ThemeProvider>
      </BrowserRouter>
    </QueryClientProvider>,
  );

beforeEach(() => {
  data.searchQuery = "";
});

describe("EvalPickerDrawer — Added evaluations", () => {
  it("leaves the list unchanged without addedEvals", () => {
    renderDrawer({});
    expect(screen.queryByText("Added evaluations")).toBeNull();
    ["Alpha eval", "Beta eval", "Gamma eval"].forEach((n) =>
      expect(screen.getByText(n)).toBeInTheDocument(),
    );
  });

  it("collapses the added ones into a counted box and leaves them out of the list, by id or by name", () => {
    renderDrawer({
      addedEvals: [
        { id: "tpl-a", name: "renamed alpha", meta: "Agent" },
        { id: "unknown-id", name: "Beta eval" },
      ],
    });
    const header = screen.getByRole("button", { name: /Added evaluations/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(within(header).getByText("2")).toBeInTheDocument();
    expect(screen.queryByText("Alpha eval")).toBeNull();
    expect(screen.queryByText("Beta eval")).toBeNull();
    expect(screen.getByText("Gamma eval")).toBeInTheDocument();
    fireEvent.click(header);
    expect(screen.getByText("renamed alpha")).toBeInTheDocument();
    expect(screen.getByText("Agent")).toBeInTheDocument();
  });

  it("opens the box when the search matches an added eval", () => {
    data.searchQuery = "renamed";
    renderDrawer({ addedEvals: [{ id: "tpl-a", name: "renamed alpha" }] });
    expect(
      screen.getByRole("button", { name: /Added evaluations/ }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("renamed alpha")).toBeInTheDocument();
  });

  it("renders the optional row action and hands it the added eval", () => {
    const onClick = vi.fn();
    renderDrawer({
      addedEvals: [{ id: "tpl-a", name: "renamed alpha" }],
      addedEvalAction: { label: "Grade this run", onClick },
    });
    fireEvent.click(screen.getByRole("button", { name: /Added evaluations/ }));
    fireEvent.click(screen.getByRole("button", { name: "Grade this run" }));
    expect(onClick).toHaveBeenCalledWith({
      id: "tpl-a",
      name: "renamed alpha",
    });
  });

  it("hides the row action where show() says no", () => {
    renderDrawer({
      addedEvals: [
        { id: "tpl-a", name: "renamed alpha", canGrade: true },
        { id: "tpl-b", name: "result column", canGrade: false },
      ],
      addedEvalAction: {
        label: "Grade this run",
        onClick: vi.fn(),
        show: (e) => e.canGrade,
      },
    });
    fireEvent.click(screen.getByRole("button", { name: /Added evaluations/ }));
    expect(
      screen.getAllByRole("button", { name: "Grade this run" }),
    ).toHaveLength(1);
  });

  // F2 — with the box off (addedEvals absent), existingEvals must still
  // disable already-added rows in the plain list the way every other
  // caller relies on today; the box's own id/name filter must not apply
  // in that case.
  it("keeps already-added rows as disabled 'Added' rows when addedEvals is absent", () => {
    renderDrawer({ existingEvals: [{ id: "tpl-a" }] });
    expect(screen.getByText("Alpha eval")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Added" })).toBeDisabled();
  });

  it("hides existingEvals rows once the box is on", () => {
    renderDrawer({ existingEvals: [{ id: "tpl-a" }], addedEvals: [] });
    expect(screen.queryByText("Alpha eval")).toBeNull();
  });

  // F6 — behaviours already implemented but previously untested.
  it("matches names case-insensitively", () => {
    renderDrawer({ addedEvals: [{ id: "zz", name: "GAMMA EVAL" }] });
    expect(screen.queryByText("Gamma eval")).toBeNull();
  });

  it("narrows the box and counts n/total while searching", () => {
    data.searchQuery = "alpha";
    renderDrawer({
      addedEvals: [
        { id: "a", name: "alpha x" },
        { id: "b", name: "beta y" },
      ],
    });
    const header = screen.getByRole("button", { name: /Added evaluations/ });
    expect(within(header).getByText("1/2")).toBeInTheDocument();
    expect(screen.queryByText("beta y")).toBeNull();
  });

  it("matches meta in search", () => {
    data.searchQuery = "agent";
    renderDrawer({ addedEvals: [{ id: "a", name: "x", meta: "Agent" }] });
    expect(
      screen.getByRole("button", { name: /Added evaluations/ }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("disables the row action and shows the busy spinner on busyName", () => {
    renderDrawer({
      addedEvals: [{ id: "a", name: "x" }],
      addedEvalAction: {
        label: "Grade this run",
        onClick: vi.fn(),
        disabled: true,
        busyName: "x",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: /Added evaluations/ }));
    const btn = screen.getByRole("button", { name: "Grade this run" });
    expect(btn).toBeDisabled();
    expect(within(btn).getByRole("progressbar")).toBeInTheDocument();
  });
});
