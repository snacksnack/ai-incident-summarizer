import { NextResponse } from "next/server";
import { unstable_cache } from "next/cache";
import { ScanCommand } from "@aws-sdk/lib-dynamodb";
import { dynamo, SERVICE_REGISTRY_TABLE } from "@/lib/dynamodb";
import { Service } from "@/lib/types";

// Caching model, chosen deliberately:
//
// `cacheComponents` is not enabled in next.config.ts, so this is the previous
// caching model — `use cache` / `cacheLife` would require opting into Cache
// Components first, and `revalidate` here would be removed if that flag were
// turned on.
//
// The cache lives at the data layer rather than on the route. A route-level
// `revalidate` would let Next prerender this handler at build time, which means
// a build running without AWS credentials would bake a cached 500 into the
// deployment. Keeping the route dynamic and caching the query instead means the
// registry is still read at most once per window, but a failed read is never
// cached as if it were a valid answer.
export const dynamic = "force-dynamic";

const REVALIDATE_SECONDS = 300;

const getServices = unstable_cache(
  async (): Promise<Service[]> => {
    // Scans the registry, not the incident table. The registry holds one small
    // item per service (~10), so this cost is fixed. Scanning incidents would
    // cost proportional to total table bytes and grow without bound.
    const result = await dynamo.send(
      new ScanCommand({ TableName: SERVICE_REGISTRY_TABLE })
    );

    return ((result.Items ?? []) as Service[])
      .filter((item) => item.affected_service)
      .sort((a, b) => a.affected_service.localeCompare(b.affected_service));
  },
  ["service-registry"],
  { revalidate: REVALIDATE_SECONDS, tags: ["services"] }
);

export async function GET() {
  try {
    return NextResponse.json(await getServices());
  } catch (err) {
    console.error("DynamoDB service registry scan failed:", err);
    return NextResponse.json({ error: "Failed to fetch services" }, { status: 500 });
  }
}
