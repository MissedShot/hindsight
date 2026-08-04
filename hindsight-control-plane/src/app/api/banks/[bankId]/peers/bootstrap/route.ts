import { proxyPeerRequest } from "@/lib/peer-proxy";

export async function POST(request: Request, { params }: { params: Promise<{ bankId: string }> }) {
  const { bankId } = await params;
  return proxyPeerRequest(request, bankId, "/peers/bootstrap", {
    method: "POST",
    errorKey: "api.errors.peers.model",
    fallbackMessage: "Failed to bootstrap peers",
  });
}
