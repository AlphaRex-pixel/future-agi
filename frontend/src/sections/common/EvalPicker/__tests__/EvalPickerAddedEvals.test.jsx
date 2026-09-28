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
  { id: "tpl-a", name: "Alpha eval", template_type: "single", eval_type: "llm", output_type: "pass_fail", created_by_name: "System" },
  { id: "tpl-b", name: "Beta eval", template_type: "single", eval_type: "llm", output_type: "pass_fail", created_by_name: "System" },
  { id: "tpl-c", name: "Gamma eval", template_type: "single", eval_type: "code", output_type: "pass_fail", created_by_name: "System" },
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
vi.mock("../EvalPickerConfigFull", () => ({ default: () => <div data-testid="config-stub" /> }));

const theme = createTheme({ palette: palette("light"), spacing: (f) => `${0.25 * f}rem` });
const renderDrawer = (props) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <BrowserRouter>
        <ThemeProvider theme={theme}>
          <EvalPickerDrawer open onClose={vi.fn()} source="simulation" {...props} />
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
    ["Alpha eval", "Beta eval", "Gamma eval"].forEach((n) => expect(screen.getByText(n)).toBeInTheDocument());
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
    expect(screen.getByRole("button", { name: /Added evaluations/ })).toHaveAttribute("aria-expanded", "true");
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
    expect(onClick).toHaveBeenCalledWith({ id: "tpl-a", name: "renamed alpha" });
  });

  it("hides the row action where show() says no", () => {
    renderDrawer({
      addedEvals: [
        { id: "tpl-a", name: "renamed alpha", canGrade: true },
        { id: "tpl-b", name: "result column", canGrade: false },
      ],
      addedEvalAction: { label: "Grade this run", onClick: vi.fn(), show: (e) => e.canGrade },
    });
    fireEvent.click(screen.getByRole("button", { name: /Added evaluations/ }));
    expect(screen.getAllByRole("button", { name: "Grade this run" })).toHaveLength(1);
  });
});
