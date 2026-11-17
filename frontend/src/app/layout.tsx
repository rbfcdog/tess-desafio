import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tess | Conversas",
  description: "Conversas persistentes com autenticação, respostas em tempo real e consumo por execução.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="pt-BR">
      <body>{children}</body>
    </html>
  );
}
