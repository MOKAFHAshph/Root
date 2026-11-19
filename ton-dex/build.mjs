// Builds UNSIGNED messages only. Never receives mnemonic/private key or broadcasts.
import {readFileSync} from 'node:fs';
import {StonApiClient} from '@ston-fi/api';
import {dexFactory,Client} from '@ston-fi/sdk';
import {Address} from '@ton/core';
const NATIVE='EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c';
const USDT='EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs';
const equal=(a,b)=>Address.parse(a).equals(Address.parse(b));
const input=JSON.parse(readFileSync(0,'utf8'));
const pins=JSON.parse(readFileSync(new URL('./routers.json',import.meta.url),'utf8'));
const api=new StonApiClient();
const client=new Client({endpoint:'https://toncenter.com/api/v2/jsonRPC',apiKey:process.env.TONCENTER_API_KEY,timeout:15000});
function validate(sim){
 if(sim.router.majorVersion!==2 || !pins.some(r=>equal(r.address,sim.router.address)&&equal(r.pton_master_address,sim.router.ptonMasterAddress)&&equal(r.pton_wallet_address,sim.router.ptonWalletAddress)))throw Error('UNPINNED_ROUTER');
 if(sim.offerUnits!==input.units || BigInt(sim.minAskUnits)<=0n)throw Error('INVALID_SIMULATION');
 const offer=input.asset==='USDT'?USDT:'ton',ask=input.asset==='USDT'?'ton':USDT;
 const same=(a,b)=>b==='ton'?a==='ton'||a==='EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c':equal(a,b);
 if(!same(sim.offerAddress,offer)||!same(sim.askAddress,ask))throw Error('ASSET_MISMATCH');
}
async function build(sim,queryId){
 const contracts=dexFactory(sim.router),router=client.open(contracts.Router.create(sim.router.address));
 const proxyTon=contracts.pTON.create(sim.router.ptonMasterAddress);
 const common={userWalletAddress:input.owner,offerAmount:sim.offerUnits,minAskAmount:sim.minAskUnits,
   receiverAddress:input.owner,refundAddress:input.owner,excessesAddress:input.owner,
   queryId:BigInt(queryId),proxyTon,referralValue:0,deadline:input.deadline??Math.floor(Date.now()/1000)+180};
 const tx=input.asset==='USDT'?await router.getSwapJettonToTonTxParams({...common,offerJettonAddress:USDT,offerJettonWalletAddress:input.ownJettonWallet,askJettonWalletAddress:sim.router.ptonWalletAddress}):await router.getSwapTonToJettonTxParams({...common,askJettonAddress:USDT,offerJettonWalletAddress:sim.router.ptonWalletAddress,askJettonWalletAddress:sim.askJettonWallet});
 return {to:tx.to.toString(),value:tx.value.toString(),body:tx.body.toBoc().toString('base64')};
}
let phase='INPUT';
try{
 if(input.asset==='USDT'&&!input.ownJettonWallet)throw Error('WALLET_ADDRESS_REQUIRED');
 if(!['USDT','TON'].includes(input.asset)||!/^\d+$/.test(input.units))throw Error('INVALID_INPUT');
 if(input.mode==='quote'){
  phase='SIMULATE';const sim=await api.simulateSwap({offerAddress:input.asset==='USDT'?USDT:NATIVE,askAddress:input.asset==='USDT'?NATIVE:USDT,
    offerUnits:input.units,slippageTolerance:input.slippage,dexV2:true});
  validate(sim);phase='BUILD';const tx=await build(sim,'0');
  process.stdout.write(JSON.stringify({simulation:sim,transaction:tx}));
 }else if(input.mode==='build'){
  validate(input.simulation);const tx=await build(input.simulation,input.query_id);
  process.stdout.write(JSON.stringify({transaction:tx}));
 }else throw Error('INVALID_MODE');
}catch(error){process.stdout.write(JSON.stringify({error: /^[A-Z_]+$/.test(error.message)?error.message:'DEX_REQUEST_FAILED',status:error.statusCode??error.response?.status??null,phase}));process.exitCode=1;}
