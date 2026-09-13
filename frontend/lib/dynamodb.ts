import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";
import { awsCredentialsProvider } from "@vercel/functions/oidc";

// No static AWS keys. Vercel injects a short-lived OIDC token
// (VERCEL_OIDC_TOKEN) into every invocation; the provider exchanges it with
// STS for temporary credentials on the read-only role that template.yaml
// defines (DashboardReadRole), whose trust policy accepts only this Vercel
// project's production and development tokens. Locally, `vercel env pull
// .env.local` provisions a token good for about 12 hours; an auth error
// mid-session means it expired, so pull again (RC1-220).
const client = new DynamoDBClient({
  region: process.env.AWS_REGION ?? "us-east-1",
  credentials: awsCredentialsProvider({
    roleArn: process.env.AWS_ROLE_ARN!,
    roleSessionName: "incident-dashboard",
  }),
});

export const dynamo = DynamoDBDocumentClient.from(client);
export const TABLE = process.env.INCIDENT_TABLE_NAME!;
export const SERVICE_REGISTRY_TABLE = process.env.SERVICE_REGISTRY_TABLE_NAME!;
