import type { Metadata } from "next";
import { SharedConversation } from "@/components/shared-conversation";

export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: "Conversa compartilhada | Tess",
  description: "Snapshot de conversa compartilhado, somente leitura.",
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

export default async function SharePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <SharedConversation key={token} token={token} />;
}
