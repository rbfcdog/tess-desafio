import type { Attachment, History } from "./chat-api";

export const attachmentTypes = ["image/png", "image/jpeg", "image/webp", "application/pdf"];
export const maxAttachmentBytes = 5 * 1024 * 1024;

export function validateUploads(files: readonly Pick<File, "name" | "size" | "type">[], selectedCount: number): string | null {
  if (files.length + selectedCount > 5) return "Selecione no máximo 5 arquivos por mensagem.";
  for (const file of files) {
    if (!attachmentTypes.includes(file.type)) return `${file.name}: use PNG, JPEG, WebP ou PDF.`;
    if (file.size <= 0 || file.size > maxAttachmentBytes) return `${file.name}: o arquivo deve ter entre 1 byte e 5 MB.`;
  }
  return null;
}

export function retainUnsentAttachments(selected: Attachment[], messages: History["messages"]): Attachment[] {
  return selected.filter((attachment) => !messages.some((message) => message.attachments?.some((sent) => sent.id === attachment.id)));
}
