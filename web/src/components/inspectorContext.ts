import { createContext, useContext } from 'react';
export const InspectorContext=createContext({wide:false,narrow:true,toggleWide:()=>{}});
export const useInspector=()=>useContext(InspectorContext);
