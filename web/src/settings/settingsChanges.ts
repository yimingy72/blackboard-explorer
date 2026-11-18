import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ApiError } from '../api/client';

export type ChangeTracking = { onDirtyChange: (dirty: boolean) => void; onBusyChange: (busy: boolean) => void };

export function useTrackSettings(dirty: boolean, busy: boolean, { onDirtyChange, onBusyChange }: ChangeTracking) {
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false); }, [busy, onBusyChange]);
}

export function useSettingsLogin(error: unknown) {
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) navigate('/login', { replace: true, state: { from: pathname + search } });
  }, [error, navigate, pathname, search]);
}
