import { proxyPeerRequest } from "@/lib/peer-proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string }> }
) {
  const { bankId, peerId } = await params;
  const query = new URL(request.url).search;
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/context${query}`,
    {
      errorKey: "api.errors.peers.context",
      fallbackMessage: "Failed to fetch peer context",
    }
  );
}
