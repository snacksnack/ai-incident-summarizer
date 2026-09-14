import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Incident } from "@/lib/types";
import IncidentListPage from "./page";

// The logic RC1-217 added and verified only by hand: typeahead matching, the
// tag/search-box sync, stale-response dropping and the summary snippet. Each
// test stubs `fetch`; nothing here reaches a route handler.

const SERVICES = ["auth-service", "payments-service", "search-api"];

function incident(overrides: Partial<Incident> = {}): Incident {
  return {
    incident_id: "INC-1",
    affected_service: "payments-service",
    severity: "high",
    status: "open",
    created_at: "2026-09-14T10:00:00+00:00",
    source_alerts: [],
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

type Deferred = { resolve: (body: unknown) => void; url: string };

// Answers `/api/services` immediately and records every `/api/incidents`
// request. By default those resolve at once; a test that needs to control
// response order sets `holdIncidents` and resolves them by hand.
function stubFetch(incidents: Incident[] = [incident()], holdIncidents = false) {
  const incidentRequests: Deferred[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/services")) {
      return Promise.resolve(jsonResponse(SERVICES.map((s) => ({ affected_service: s }))));
    }
    if (holdIncidents) {
      return new Promise<Response>((resolve) => {
        incidentRequests.push({ url, resolve: (body) => resolve(jsonResponse(body)) });
      });
    }
    incidentRequests.push({ url, resolve: () => undefined });
    return Promise.resolve(jsonResponse(incidents));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, incidentRequests };
}

function incidentUrls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.startsWith("/api/incidents"));
}

async function renderPage() {
  const user = userEvent.setup();
  render(<IncidentListPage />);
  // Tags appear once the services request resolves.
  await screen.findByRole("button", { name: "payments-service" });
  return user;
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  // Vitest globals are off, so Testing Library does not unmount between tests.
  cleanup();
  vi.unstubAllGlobals();
});

describe("service typeahead", () => {
  it("filters the tag row by case-insensitive substring without querying", async () => {
    const { fetchMock } = stubFetch();
    const user = await renderPage();

    await user.type(screen.getByLabelText("Service"), "PAY");

    const tags = screen.getAllByRole("button").map((b) => b.textContent);
    expect(tags).toEqual(["payments-service"]);
    // A partial match narrows the tags; it is not a filter on the incidents.
    expect(incidentUrls(fetchMock)).toEqual(["/api/incidents?status=open"]);
  });

  it("selects the service when the full name is typed in any case", async () => {
    const { fetchMock } = stubFetch();
    const user = await renderPage();

    await user.type(screen.getByLabelText("Service"), "Payments-Service");

    expect(screen.getByRole("button", { name: "payments-service" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
    expect(incidentUrls(fetchMock).at(-1)).toBe(
      "/api/incidents?status=open&service=payments-service"
    );
  });

  it("says no services match instead of showing an empty table", async () => {
    stubFetch([incident({ incident_id: "INC-7" })]);
    const user = await renderPage();
    await screen.findByRole("table");

    await user.type(screen.getByLabelText("Service"), "zzz");

    expect(screen.getByText("No services match “zzz”.")).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    // The incident list is untouched: nothing was selected, so nothing re-queried.
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2);
  });
});

describe("tag toggle and search-box sync", () => {
  it("clicking a tag selects it and writes the name into the search box", async () => {
    const { fetchMock } = stubFetch();
    const user = await renderPage();

    await user.click(screen.getByRole("button", { name: "auth-service" }));

    expect(screen.getByLabelText("Service")).toHaveValue("auth-service");
    expect(screen.getByRole("button", { name: "auth-service" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
    expect(incidentUrls(fetchMock).at(-1)).toBe("/api/incidents?status=open&service=auth-service");
  });

  it("clicking the active tag clears both controls", async () => {
    const { fetchMock } = stubFetch();
    const user = await renderPage();

    await user.click(screen.getByRole("button", { name: "auth-service" }));
    await user.click(screen.getByRole("button", { name: "auth-service" }));

    expect(screen.getByLabelText("Service")).toHaveValue("");
    expect(screen.getByRole("button", { name: "auth-service" })).toHaveAttribute(
      "aria-pressed",
      "false"
    );
    expect(incidentUrls(fetchMock).at(-1)).toBe("/api/incidents?status=open");
  });

  it("typing after a click clears the previous selection", async () => {
    const { fetchMock } = stubFetch();
    const user = await renderPage();

    await user.click(screen.getByRole("button", { name: "auth-service" }));
    await user.type(screen.getByLabelText("Service"), "x");

    // "auth-servicex" matches nothing exactly, so no service is selected and
    // the two controls cannot disagree about which one is.
    expect(screen.getByLabelText("Service")).toHaveValue("auth-servicex");
    expect(screen.queryByRole("button", { pressed: true })).not.toBeInTheDocument();
    expect(incidentUrls(fetchMock).at(-1)).toBe("/api/incidents?status=open");
  });
});

describe("stale responses", () => {
  it("never renders an older result set over a newer one", async () => {
    const { incidentRequests } = stubFetch([], true);
    const user = await renderPage();
    expect(incidentRequests.map((r) => r.url)).toEqual(["/api/incidents?status=open"]);

    await user.selectOptions(screen.getByLabelText("Status"), "resolved");
    expect(incidentRequests.map((r) => r.url)).toEqual([
      "/api/incidents?status=open",
      "/api/incidents?status=resolved",
    ]);

    // The newer request answers first, then the superseded one arrives late.
    incidentRequests[1].resolve([
      incident({ incident_id: "INC-new", affected_service: "search-api", status: "resolved" }),
    ]);
    await screen.findByText("search-api", { selector: "a" });

    incidentRequests[0].resolve([
      incident({ incident_id: "INC-old", affected_service: "auth-service", status: "open" }),
    ]);
    // Let the late promise settle; the table must not change.
    await new Promise((r) => setTimeout(r, 0));

    expect(screen.getByText("search-api", { selector: "a" })).toBeInTheDocument();
    expect(screen.queryByText("auth-service", { selector: "a" })).not.toBeInTheDocument();
  });
});

describe("summary snippet", () => {
  it("renders an em dash for a malformed or absent llm_summary", async () => {
    stubFetch([
      incident({ incident_id: "INC-1", llm_summary: JSON.stringify({ summary: "Disk full on db-1." }) }),
      incident({ incident_id: "INC-2", llm_summary: "not json {" }),
      incident({ incident_id: "INC-3", llm_summary: undefined }),
      incident({ incident_id: "INC-4", llm_summary: JSON.stringify({ likely_cause: "no summary key" }) }),
    ]);
    await renderPage();
    await screen.findByRole("table");

    const summaries = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell").at(-1)?.textContent);
    expect(summaries).toEqual(["Disk full on db-1.", "—", "—", ""]);
  });

  it("truncates a long summary to 120 characters with an ellipsis", async () => {
    const long = "x".repeat(200);
    stubFetch([incident({ llm_summary: JSON.stringify({ summary: long }) })]);
    await renderPage();
    await screen.findByRole("table");

    const cell = within(screen.getAllByRole("row")[1]).getAllByRole("cell").at(-1);
    expect(cell?.textContent).toBe("x".repeat(117) + "…");
  });
});
