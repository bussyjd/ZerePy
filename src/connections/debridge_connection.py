import os
import logging
import requests
from typing import Dict, Any, List
from dotenv import load_dotenv
from src.connections.base_connection import BaseConnection, Action, ActionParameter
from src.connections.solana_connection import SolanaConnection
from src.connections.evm_connection import EVMConnection
from src.connections.sonic_connection import SonicConnection

logger = logging.getLogger("connections.debridge_connection")

class DeBridgeConnectionError(Exception):
    """Base exception for DeBridge connection errors"""
    pass

class DeBridgeAPIError(DeBridgeConnectionError):
    """Raised when DeBridge API requests fail"""
    pass

class DeBridgeConnection(BaseConnection):

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        load_dotenv()
        self.api_url = os.getenv("DEBRIDGE_API_URL", "https://dln.debridge.finance/v1.0")
        self._session = requests.Session()
        self._chain_connection = None
        self._chain_type = None
        
    def set_chain_connection(self, connection: Any):
        if hasattr(connection, 'aggregator_api'):  # Sonic-specific attribute
            self._chain_type = 'sonic'
        elif hasattr(connection, '_web3'): 
            self._chain_type = 'evm'
        elif hasattr(connection, '_get_connection_async'):
            self._chain_type = 'solana'
        else:
            raise ValueError("Unsupported chain type")
        self._chain_connection = connection

    async def execute_bridge_tx(self, dest_chain_id: int, dest_receiver: str, 
                                src_asset: str, src_amount: float):
        if not self._chain_connection:
            raise ValueError("No chain connection configured")

        # Chain-agnostic address retrieval
        sender_addr = self._get_sender_address()
        
        # Unified fee quoting
        quote = await self._get_fee_quote(sender_addr, dest_chain_id, dest_receiver,
                                        src_asset, src_amount)
                                        
        # Chain-specific TX building
        if self._chain_type == 'evm':
            tx_data = self._build_evm_tx(src_asset, src_amount, quote)
        elif self._chain_type == 'solana':
            tx_data = await self._build_solana_tx(src_asset, src_amount, quote)
            
        return await self._send_transaction(tx_data)

    def _get_sender_address(self) -> str:
        """Universal address getter"""
        if self._chain_type == 'evm':
            return self._chain_connection.get_address()
        elif self._chain_type == 'solana':
            return str(self._chain_connection._get_wallet().public_key)
            
    async def _send_transaction(self, tx_data: Dict) -> str:
        """Unified transaction sending"""
        if self._chain_type == 'evm' or self._chain_type == 'sonic':
            return self._chain_connection.send_transaction(tx_data)
        elif self._chain_type == 'solana':
            async with self._chain_connection._get_connection_async() as conn:
                return await conn.send_transaction(tx_data)

    def set_solana_connection(self, connection: SolanaConnection):
        """Set the Solana connection from the connection manager"""
        self.solana_connection = connection

    def register_actions(self) -> None:
        """Register available DeBridge actions"""
        self.actions = {
            "create_bridge_tx": Action(
                name="create_bridge_tx",
                parameters=[
                    ActionParameter("srcChainId", True, str, "Source chain ID"),
                    ActionParameter("srcChainTokenIn", True, str, "Source token address"),
                    ActionParameter("srcChainTokenInAmount", True, str, "Amount to bridge"),
                    ActionParameter("dstChainId", True, str, "Destination chain ID"),
                    ActionParameter("dstChainTokenOut", True, str, "Destination token address"),
                    ActionParameter("dstChainTokenOutRecipient", True, str, "Destination chain address to receive tokens")
                ],
                description="Create a cross-chain bridging transaction"
            ),
            "get_tokens_info": Action(
                name="get_tokens_info",
                parameters=[
                    ActionParameter("chainId", True, str, "Chain ID to get token information for"),
                    ActionParameter("search", False, str, "Optional search term to filter tokens by name or symbol")
                ],
                description="Get information about tokens available for cross-chain bridging"
            ),
            "get_supported_chains": Action(
                name="get_supported_chains",
                parameters=[],  # No parameters needed
                description="Get list of chains supported by deBridge for cross-chain transfers"
            ),
            "execute_bridge_tx": Action(
                name="execute_bridge_tx",
                parameters=[],  # No parameters needed, will use stored tx data
                description="Execute a previously created bridge transaction"
            )
        }

    @property
    def is_llm_provider(self) -> bool:
        return False

    def validate_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Validate DeBridge configuration"""
        if "api_url" not in config:
            config["api_url"] = os.getenv("DEBRIDGE_API_URL", "https://dln.debridge.finance/v1.0")
        return config

    def _get_solana_address(self) -> str:
        """Get Solana wallet public key"""
        wallet = self.solana_connection._get_wallet()
        return str(wallet.pubkey())

    def _make_request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        """Make HTTP request with error handling"""
        headers = {"accept": "application/json"}
        kwargs['headers'] = headers

        logger.debug(f"Making {method.upper()} request to {url}")
        logger.debug(f"Request params: {kwargs}")
        
        try:
            response = requests.request(method, url, timeout=10, **kwargs)
            logger.debug(f"Response status: {response.status_code}")
            logger.debug(f"Response text: {response.text}")

            try:
                data = response.json()
            except ValueError:
                raise DeBridgeAPIError(f"Invalid response format: {response.text}")

            if not response.ok:
                error_msg = data.get('errorMessage', data.get('message', 'Unknown error occurred'))
                logger.error(f"API error: {error_msg}")
                raise DeBridgeAPIError(f"API error: {error_msg}")

            logger.debug(f"Request successful: {response.status_code}")
            return data

        except requests.Timeout:
            raise DeBridgeAPIError("Request timed out")
            
        except requests.ConnectionError as e:
            raise DeBridgeAPIError(f"Connection error: {str(e)}")
            
        except requests.RequestException as e:
            raise DeBridgeAPIError(str(e))

    def create_bridge_tx(self, srcChainId: str,
                        srcChainTokenIn: str,
                        srcChainTokenInAmount: str,
                        dstChainId: str,
                        dstChainTokenOut: str,
                        dstChainTokenOutRecipient: str,
                        dstChainTokenOutAmount: str = "auto",
                        affiliateFeePercent: str = "0",
                        prependOperatingExpenses: bool = True) -> Dict[str, Any]:
        """
        Create a cross-chain bridging transaction
        """
        if not dstChainTokenOutRecipient:
            raise DeBridgeConnectionError("dstChainTokenOutRecipient is required")
            
        # Get Solana wallet address for source chain parameters
        solana_address = self._get_solana_address()
        
        params = {
            "srcChainId": srcChainId,
            "srcChainTokenIn": srcChainTokenIn,
            "srcChainTokenInAmount": srcChainTokenInAmount,
            "dstChainId": dstChainId,
            "dstChainTokenOut": dstChainTokenOut,
            "dstChainTokenOutAmount": dstChainTokenOutAmount,
            "affiliateFeePercent": affiliateFeePercent,
            "prependOperatingExpenses": str(prependOperatingExpenses).lower(),
            **({"skipSolanaRecipientValidation": "true"} if self._chain_type == 'solana' else {}),  # Optional Solana recipient validation
            "referralCode": "21064",  # Analytics
            "deBridgeApp": "ZEREPY",  # Analytics
            "dstChainTokenOutRecipient": dstChainTokenOutRecipient,  # Required destination address
            "srcChainOrderAuthorityAddress": solana_address,  # Always use source chain address
            "dstChainOrderAuthorityAddress": dstChainTokenOutRecipient  # Use destination address
        }

        result = self._make_request(
            "GET",
            f"{self.api_url}/dln/order/create-tx",
            params=params
        )
        
        # Store the transaction for later execution
        self._last_created_tx = result
        logger.debug(f"Full response from DeBridge: {result}")
        logger.debug(f"Transaction data structure: {result.get('tx', {})}")

        return result

    def is_configured(self, verbose: bool = False) -> bool:
        """Check if DeBridge connection is configured"""
        try:
            # Test API connection by getting supported chains
            response = self._make_request("GET", f"{self.api_url}/supported-chains-info")
            if verbose:
                logger.info("DeBridge connection is configured and working")
            return True
        except Exception as e:
            if verbose:
                logger.error(f"DeBridge connection is not configured or not working: {str(e)}")
            return False

    def configure(self) -> bool:
        """Configure the DeBridge connection"""
        try:
            # Test API connection
            response = self._make_request("GET", f"{self.api_url}/supported-chains-info")
            logger.info("DeBridge API connection successful")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to DeBridge API: {str(e)}")
            return False

    def perform_action(self, action_name: str, kwargs: Dict[str, Any]) -> Any:
        """Execute a DeBridge action with validation"""
        if not self.is_configured():
            raise DeBridgeConnectionError("DeBridge connection is not configured")

        if action_name not in self.actions:
            raise DeBridgeConnectionError(f"Unknown action: {action_name}")

        method_name = action_name
        method = getattr(self, method_name)
        
        return method(**kwargs)

    def get_supported_chains(self, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Get list of chains supported by deBridge protocol
        Returns: List of supported chains with their configurations
        """
        try:
            response = self._make_request("GET", f"{self.api_url}/supported-chains-info")
            if response.get("error"):
                raise DeBridgeAPIError(f"API Error: {response['error']}")
            return response
            
        except Exception as e:
            logger.error(f"Failed to fetch supported chains: {str(e)}")
            raise DeBridgeAPIError(f"Failed to fetch supported chains: {str(e)}")

    def get_tokens_info(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get token information for a specific chain
        Returns: Token information including name, symbol, and decimals
        """
        try:
            response = self._make_request(
                "GET",
                f"{self.api_url}/token-list",
                params={"chainId": params["chainId"]}
            )
            
            if response.get("error"):
                raise DeBridgeAPIError(f"API Error: {response['error']}")
            
            # Extract token data
            tokens = response.get("tokens", [])
            
            # If search query provided, filter tokens
            if "search" in params:
                search_term = params["search"].lower()
                tokens = [
                    token for token in tokens
                    if search_term in token.get("name", "").lower() or
                    search_term in token.get("symbol", "").lower() or
                    search_term in token.get("address", "").lower()
                ]
            
            # Limit results if specified
            if "limit" in params:
                try:
                    limit = int(params["limit"])
                    tokens = tokens[:limit]
                except (ValueError, TypeError):
                    pass  # Ignore invalid limit
                    
            return {
                "status": "success",
                "tokens": tokens,
                "count": len(tokens)
            }
            
        except Exception as e:
            logger.error(f"Failed to fetch token information: {str(e)}")
            raise DeBridgeAPIError(f"Failed to fetch token information: {str(e)}")

    async def execute_bridge_tx(self, params: Dict):
        try:
            if not self._chain_connection:
                raise DeBridgeConnectionError("No chain connection configured")
                
            tx_data = params.get("tx_data")
            if not tx_data:
                raise DeBridgeConnectionError("Missing transaction data")

            # Unified response format
            response = {"orderId": tx_data.get("orderId")}
            
            if self._chain_type == "evm" or self._chain_type == "sonic":
                signed_tx = self._chain_connection.sign_transaction(tx_data)
                response["txHash"] = self._chain_connection.send_transaction(signed_tx)
            elif self._chain_type == "solana":
                tx_buffer = bytes.fromhex(tx_data["tx"]["data"][2:])
                transaction = self._chain_connection.create_versioned_transaction(tx_buffer)
                response["signature"] = await self._chain_connection.send_transaction(
                    transaction, 
                    opts={
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed",
                        "maxRetries": 3
                    }
                )
            else:
                raise DeBridgeConnectionError(f"Unsupported chain type: {self._chain_type}")
                
            return {
                "signature": response["signature"],
                "orderId": tx_data.get("orderId")
            }

        except Exception as e:
            logger.error(f"Bridge execution failed: {str(e)}")
            raise DeBridgeConnectionError("Bridge transaction failed") from e
