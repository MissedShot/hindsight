import { proxyPeerRequest, invalidPeerJsonResponse } from "@/lib/peer-proxy";

export async function GET(request: Request, { params }: { params: Promise<{ bankId: string }> }) {
  const { bankId } = await params;
  return proxyPeerRequest(request, bankId, "/peers", {
    errorKey: "api.errors.peers.list",
    fallbackMessage: "Failed to list peers",
  });
}

export async function POST(request: Request, { params }: { params: Promise<{ bankId: string }> }) {
  const { bankId } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return invalidPeerJsonResponse(request);
  }

  return proxyPeerRequest(request, bankId, "/peers", {
    method: "POST",
    body,
    errorKey: "api.errors.peers.create",
    fallbackMessage: "Failed to create peer",
  });
}
