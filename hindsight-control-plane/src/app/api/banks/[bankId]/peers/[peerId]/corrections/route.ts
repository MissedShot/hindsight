import { proxyPeerRequest, invalidPeerJsonResponse } from "@/lib/peer-proxy";

export async function POST(
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

  const query = new URL(request.url).search;
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/corrections${query}`,
    {
      method: "POST",
      body,
      errorKey: "api.errors.peers.correction",
      fallbackMessage: "Failed to create peer correction",
    }
  );
}
