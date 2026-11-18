import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tess | Conversas",
  description: "Conversas persistentes com autenticação, respostas em tempo real e consumo por execução.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="pt-BR">
      <head>
        <script dangerouslySetInnerHTML={{ __html: "try{var t=localStorage.getItem('tess:theme');document.documentElement.dataset.theme=t==='dark'||t==='light'?t:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')}catch(e){}" }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
