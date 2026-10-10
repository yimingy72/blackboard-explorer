import React, { useEffect, useState } from 'react';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp } from 'antd';
import { XProvider } from '@ant-design/x';
import zhCN from 'antd/locale/zh_CN';
import xZhCN from '@ant-design/x/locale/zh_CN';
import App from './App';
import { appTheme } from './styles/theme';
import 'antd/dist/reset.css';
import './styles/global.css';

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
export default function AppRoot() {
  const [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [narrow, setNarrow] = useState(() => matchMedia('(max-width: 899px)').matches);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => { const media = matchMedia('(max-width: 899px)'); const update = () => setNarrow(media.matches); media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, []);
  return <XProvider locale={{ ...zhCN, ...xZhCN }} virtual={false} button={{autoInsertSpace:false}} theme={appTheme(reduced, narrow)}>
    <AntApp><QueryClientProvider client={queryClient}><BrowserRouter><App /></BrowserRouter></QueryClientProvider></AntApp>
  </XProvider>;
}
