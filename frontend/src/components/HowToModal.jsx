// frontend/src/components/HowToModal.jsx
import React, { useEffect, useRef } from 'react';

export default function HowToModal({ open, onClose }) {
  const closeBtnRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === 'Escape') onClose?.(); };
    window.addEventListener('keydown', onKey);

    // lock background scroll
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // focus close button on open
    setTimeout(() => closeBtnRef.current?.focus(), 0);

    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open) return null;

  const stop = (e) => e.stopPropagation();

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div
        className="modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="howto-title"
        onClick={stop}
      >
        <div className="modal-header">
          <h3 id="howto-title">How to Use</h3>
          <button
            className="modal-close"
            onClick={onClose}
            aria-label="Close"
            ref={closeBtnRef}
          >
            ×
          </button>
        </div>

        <div className="modal-body">
          <p>This app lets you talk to an avatar that answers using your own documents (RAG). Here’s the quick guide:</p>

          <ol className="howto-steps">
            <li>
              <strong>Upload documents (ingestion)</strong><br/>
              Click <em>Upload</em> and choose one or more files. Supported formats: <code>PDF</code>, <code>DOCX</code>, <code>TXT</code>, <code>MD</code>.
              The text is chunked and indexed into Pinecone under your private namespace.
            </li>

            <li>
              <strong>Select docs for RAG (optional)</strong><br/>
              Click <em>Select docs</em> to choose specific files to constrain retrieval. If you select none, the assistant considers all uploaded docs.
            </li>

            <li>
              <strong>View your docs</strong><br/>
              Click <em>View</em> to see the list of files in your namespace (the app’s memory of your uploaded docs).
            </li>

            <li>
              <strong>Remove a document</strong><br/>
              Click <em>Remove</em>, choose the file(s) to delete, and confirm. This removes their chunks from the vector index and the docs list.
            </li>

            <li>
              <strong>Clear conversation memory</strong><br/>
              Click <em>Clear memory</em> to wipe the chat memory index for your namespace. (Your uploaded docs remain; only the running conversation context is cleared.)
            </li>

            <li>
              <strong>Temperature (tone/creativity)</strong><br/>
              Use the temperature toggle/slider to make answers more precise (lower) or more creative (higher). It affects how the LLM writes the final response.
            </li>

            <li>
              <strong>Ask your question</strong><br/>
              Type in the prompt box and press Enter. If relevant docs exist, the app shows short RAG excerpts (with file name & page) above the prompt, then the avatar speaks a concise, conversational answer that naturally refers to those excerpts. If no docs are uploaded, it answers directly from the model.
            </li>

            <li>
              <strong>Mobile tips</strong><br/>
              On phones, the avatar is on the top half; the controls are below—scroll down to access them. After you press Enter, the view scrolls back up so you can watch the avatar speak.
            </li>
          </ol>
        </div>

        <div className="modal-footer">
          <button className="btn-primary" onClick={onClose}>Got it</button>
        </div>
      </div>
    </div>
  );
}

