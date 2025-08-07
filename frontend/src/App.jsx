// frontend/src/App.jsx
import React, { useState, useEffect } from 'react';
import AvatarCanvas from './components/AvatarCanvas';
import ChatBox from './components/ChatBox';
import Login from './components/Login';
import Register from './components/Register';
import HowToModal from './components/HowToModal.jsx';
import { setMorphIndexHints } from './utils/animationUtils';
import './styles.css';

export default function App() {
  const [token, setToken] = useState(localStorage.getItem('token'));
  const [showRegister, setShowRegister] = useState(false);

  const [expression, setExpression] = useState(null);
  const [viseme, setViseme] = useState(null);
  const [gesture, setGesture] = useState(null);
  const [speaking, setSpeaking] = useState(false);

  const [showHelp, setShowHelp] = useState(false);     // <-- NEW

  const handleLogout = () => {
    localStorage.removeItem('token');
    setToken(null);
  };

  useEffect(() => {
    fetch('/avatar.morphmap.json')
      .then(r => r.ok ? r.json() : null)
      .then(data => data && setMorphIndexHints(data))
      .catch(() => {});
  }, []);

  if (!token) {
    return (
      <div className="auth-container">
        {showRegister ? (
          <Register onToggle={() => setShowRegister(false)} />
        ) : (
          <Login
            onToggle={() => setShowRegister(true)}
            onLogin={(tok) => {
              localStorage.setItem('token', tok);
              setToken(tok);
            }}
          />
        )}
      </div>
    );
  }

  return (
    <>
      <div className="app">
        {/* LEFT: Avatar pane with GitHub link overlay */}
        <div className="left-pane">
          {/* Small overlay link (top-right) */}
          <a
            className="gh-link"
            href="https://github.com/alvesmh"
            target="_blank"
            rel="noopener noreferrer"
            title="Source code on GitHub"
          >
            see code on GitHub
          </a>

          <AvatarCanvas
            expression={expression}
            viseme={viseme}
            gesture={gesture}
            speaking={speaking}
          />
        </div>

        {/* RIGHT: RAG + Controls */}
        <div className="right-pane">
          <div className="header-row">
            {/* Replaces the old <h2>AI Assistant</h2> */}
            <button className="link-button" onClick={() => setShowHelp(true)}>
              How to Use
            </button>
            <button onClick={handleLogout}>Logout</button>
          </div>

          <ChatBox
            token={token}
            setExpression={setExpression}
            setViseme={setViseme}
            setGesture={setGesture}
            setSpeaking={setSpeaking}
          />
        </div>
      </div>

      {/* Floating help modal */}
      <HowToModal open={showHelp} onClose={() => setShowHelp(false)} />
    </>
  );
}

