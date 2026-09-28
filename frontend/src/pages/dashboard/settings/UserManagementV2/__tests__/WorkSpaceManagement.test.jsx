import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HelmetProvider } from "react-helmet-async";

import { render } from "src/utils/test-utils";
import { OPENAPI_CONTRACT } from "src/api/contracts/openapi-contract.generated";

const mocks = vi.hoisted(() => ({
  post: vi.fn(),
  enqueueSnackbar: vi.fn(),
}));

vi.mock("src/utils/axios", () => ({
  default: { post: mocks.post, get: vi.fn() },
  endpoints: { workspaces: { create: "/accounts/workspaces/" } },
}));

vi.mock("notistack", () => ({
  useSnackbar: () => ({ enqueueSnackbar: mocks.enqueueSnackbar }),
}));

vi.mock("src/contexts/OrganizationContext", () => ({
  useOrganization: () => ({ orgLevel: 15 }),
}));

vi.mock("../GridTable", () => ({
  default: React.forwardRef(function GridTable() {
    return null;
  }),
}));
vi.mock("../AllActionForm", () => ({ default: () => null }));
vi.mock("src/components/iconify", () => ({ default: () => null }));

import WorkSpaceManagement from "../WorkSpaceManagement";

const renderPage = () =>
  render(
    <HelmetProvider>
      <QueryClientProvider client={new QueryClient()}>
        <WorkSpaceManagement />
      </QueryClientProvider>
    </HelmetProvider>,
  );

describe("WorkSpaceManagement create workspace", () => {
  beforeEach(() => {
    mocks.post.mockReset();
    mocks.enqueueSnackbar.mockReset();
  });

  it("sends only fields the backend create contract accepts", async () => {
    mocks.post.mockResolvedValueOnce({ data: { status: true, result: {} } });
    renderPage();

    fireEvent.click(
      screen.getByRole("button", { name: "Create New Workspace" }),
    );
    fireEvent.change(screen.getByPlaceholderText("Enter workspace name"), {
      target: { value: "  Research  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(mocks.post).toHaveBeenCalledTimes(1));
    const [url, payload] = mocks.post.mock.calls[0];
    expect(url).toBe("/accounts/workspaces/");
    expect(payload).toMatchObject({
      name: "Research",
      display_name: "Research",
    });

    // The backend rejects unknown keys with 400 "Unknown field.", so every
    // key must be declared on the WorkspaceCreateRequest serializer.
    const allowed = Object.keys(
      OPENAPI_CONTRACT.definitions.WorkspaceCreateRequest.properties,
    );
    expect(allowed.length).toBeGreaterThan(0);
    Object.keys(payload).forEach((key) => expect(allowed).toContain(key));
  });
});
