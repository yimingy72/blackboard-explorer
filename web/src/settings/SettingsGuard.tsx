import { Modal } from 'antd';
import { useEffect } from 'react';
import { useBlocker } from 'react-router-dom';

export function SettingsGuard({ dirty, busy, discard }: { dirty: boolean; busy: boolean; discard?: () => void }) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    (dirty || busy) && nextLocation.pathname !== '/login' &&
    (currentLocation.pathname !== nextLocation.pathname || currentLocation.search !== nextLocation.search));
  useEffect(() => {
    if (!dirty && !busy && blocker.state === 'blocked') blocker.proceed();
  }, [blocker, dirty, busy]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const prevent = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', prevent);
    return () => window.removeEventListener('beforeunload', prevent);
  }, [dirty, busy]);
  return <Modal title="离开配置页？" open={blocker.state === 'blocked'} onCancel={() => blocker.reset?.()}
    onOk={() => { if (!busy) { discard?.(); blocker.proceed?.(); } }} okText="放弃修改并离开" cancelText="继续编辑"
    okButtonProps={{ disabled: busy }}><p>{busy ? '正在处理当前操作，请稍候。' : '尚未保存的修改会丢失。'}</p></Modal>;
}
