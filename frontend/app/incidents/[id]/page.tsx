"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Incident, LlmSummary } from "@/lib/types";

const SEVERITY_COLOURS: Record<string, string> = {
  critical: "bg-red-100 text-red-800",
  high: "bg-orange-100 text-orange-800",
  medium: "bg-yellow-100 text-yellow-800",
  low: "bg-green-100 text-green-800",
};

function Badge({ value, colours }: { value: string; colours: Record<string, string> }) {
  const cls = colours[value.toLowerCase()] ?? "bg-gray-100 text-gray-800";
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {value}
    </span>
  );
}

function parseSummary(raw?: string): LlmSummary | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export default function IncidentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [incident, setIncident] = useState<Incident | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/incidents/${id}`)
      .then((r) => {
        if (!r.ok) throw new Error("Not found");
        return r.json();
      })
      .then(setIncident)
      .catch(() => setError("Incident not found."))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) return <main className="max-w-3xl mx-auto px-4 py-8"><p className="text-sm text-gray-500">Loading…</p></main>;
  if (error || !incident) return <main className="max-w-3xl mx-auto px-4 py-8"><p className="text-sm text-red-600">{error}</p></main>;

  const summary = parseSummary(incident.llm_summary);
  const slackUrl = incident.slack_thread_id
    ? `https://slack.com/app_redirect?channel=${process.env.NEXT_PUBLIC_SLACK_CHANNEL_ID}&message_ts=${incident.slack_thread_id}`
    : null;
  const jiraUrl = incident.jira_ticket_id
    ? `${process.env.NEXT_PUBLIC_JIRA_BASE_URL}/browse/${incident.jira_ticket_id}`
    : null;

  return (
    <main className="max-w-3xl mx-auto px-4 py-8 space-y-8">
      <div>
        <Link href="/" className="text-sm text-blue-600 hover:underline">← Back to dashboard</Link>
      </div>

      {/* Header */}
      <div className="space-y-2">
        <div className="flex items-center gap-3 flex-wrap">
          <h1 className="text-2xl font-bold text-gray-900">{incident.affected_service}</h1>
          <Badge value={incident.severity} colours={SEVERITY_COLOURS} />
          <span className="text-sm text-gray-500">{incident.status}</span>
        </div>
        <p className="text-sm text-gray-500">Created: {incident.created_at}</p>
        <p className="text-xs text-gray-400 font-mono">{incident.incident_id}</p>
      </div>

      {/* LLM Summary */}
      {summary ? (
        <section className="bg-gray-50 rounded-lg p-5 space-y-4 border border-gray-200">
          <h2 className="font-semibold text-gray-800">AI Summary</h2>
          <div>
            <p className="text-xs font-semibold text-gray-500 uppercase mb-1">Summary</p>
            <p className="text-sm text-gray-700">{summary.summary}</p>
          </div>
          <div>
            <p className="text-xs font-semibold text-gray-500 uppercase mb-1">Likely cause</p>
            <p className="text-sm text-gray-700">{summary.likely_cause}</p>
          </div>
          <div>
            <p className="text-xs font-semibold text-gray-500 uppercase mb-1">Next step</p>
            <p className="text-sm text-gray-700">{summary.next_step}</p>
          </div>
        </section>
      ) : (
        <section className="bg-gray-50 rounded-lg p-5 border border-gray-200">
          <p className="text-sm text-gray-500">No LLM summary available.</p>
        </section>
      )}

      {/* Links */}
      {(slackUrl || jiraUrl) && (
        <section className="flex gap-4 flex-wrap">
          {slackUrl && (
            <a href={slackUrl} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm text-blue-600 hover:underline border border-blue-200 rounded px-3 py-1.5">
              View Slack thread →
            </a>
          )}
          {jiraUrl && (
            <a href={jiraUrl} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm text-blue-600 hover:underline border border-blue-200 rounded px-3 py-1.5">
              {incident.jira_ticket_id} in Jira →
            </a>
          )}
        </section>
      )}

      {/* Alert list */}
      <section>
        <h2 className="font-semibold text-gray-800 mb-3">Source alerts ({incident.source_alerts.length})</h2>
        <div className="space-y-2">
          {incident.source_alerts.map((alert) => (
            <div key={alert.alert_id} className="flex items-start gap-3 rounded border border-gray-200 bg-white px-4 py-3 text-sm">
              <div className="flex-1 min-w-0">
                <p className="font-medium text-gray-800">{alert.alert_name}</p>
                <p className="text-gray-500 text-xs mt-0.5">{alert.source} · {alert.received_at}</p>
              </div>
              <Badge value={alert.severity} colours={SEVERITY_COLOURS} />
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
