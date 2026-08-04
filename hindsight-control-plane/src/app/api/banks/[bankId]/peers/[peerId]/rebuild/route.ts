import { proxyPeerRequest } from "@/lib/peer-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ bankId: string; peerId: string }> }
) {
  const { bankId, peerId } = await params;
  const query = new URL(request.url).search;
  return proxyPeerRequest(
    request,
    bankId,
    `/peers/${encodeURIComponent(peerId)}/rebuild${query}`,
    {
      method: "POST",
      errorKey: "api.errors.peers.rebuild",
      fallbackMessage: "Failed to rebuild peer model",
    }
  );
}
