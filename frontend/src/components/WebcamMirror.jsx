import React, { useEffect, useRef } from 'react';

export default function WebcamMirror() {
  const videoRef = useRef();

  useEffect(() => {
    navigator.mediaDevices.getUserMedia({ video: true }).then(stream => {
      videoRef.current.srcObject = stream;
      videoRef.current.play();
    });
  }, []);

  return (
    <video ref={videoRef} style={{ width: 160, height: 120, transform: 'scaleX(-1)' }} />
  );
}
