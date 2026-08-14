import { proxyPeerRequest, invalidPeerJsonResponse } from "@/lib/peer-proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string }> }
) {
  const { bankId, peerId } = await params;
  return proxyPeerRequest(request, bankId, `/peers/${encodeURIComponent(peerId)}`, {
    errorKey: "api.errors.peers.fetch",
    fallbackMessage: "Failed to fetch peer",
  });
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string }> }
) {
  const { bankId, peerId } = await params;
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return invalidPeerJsonResponse(request);
  }

  return proxyPeerRequest(request, bankId, `/peers/${encodeURIComponent(peerId)}`, {
    method: "PATCH",
    body,
    errorKey: "api.errors.peers.update",
    fallbackMessage: "Failed to update peer",
  });
}
