import {loadAll,loadMerge} from '../api/business.js';
export const store={session:JSON.parse(localStorage.getItem('session')||'null'),data:{sources:[],periods:[],policies:[],runs:[],schedules:[],deliveries:[],audits:[],merge:{batches:[],pending:[]}},selected:null,selectedBatch:null};
export async function reload(){const [data,merge]=await Promise.all([loadAll(),loadMerge()]);store.data={...data,merge}}
export function setSession(value){store.session=value;if(value)localStorage.setItem('session',JSON.stringify(value));else localStorage.removeItem('session')}
