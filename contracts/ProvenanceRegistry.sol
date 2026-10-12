// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/AccessControl.sol";

/// @title ProvenanceRegistry — lot commitments, batch anchors, freeze + actor revocation
/// @notice Read-public / write-permissioned. Verifier needs no key.
contract ProvenanceRegistry is AccessControl {
    bytes32 public constant REGISTRAR_ROLE = keccak256("REGISTRAR_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");

    struct Lot {
        address manufacturer;
        bytes32 serialsRoot;
        uint32 quantity;
        uint64 ts;
    }

    struct Anchor {
        bytes32 root;
        uint32 count;
        uint64 ts;
    }

    mapping(bytes32 => Lot) public lots;
    mapping(bytes32 => bool) public lotFrozen;
    mapping(address => bool) public activeActor;

    Anchor[] public anchors;

    event LotRegistered(bytes32 indexed lotId, address manufacturer, bytes32 root, uint32 quantity);
    event BatchAnchored(uint256 indexed anchorId, bytes32 root, uint32 count);
    event LotFrozen(bytes32 indexed lotId, bool value);
    event ActorSet(address indexed actor, bool active);

    constructor(address admin) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(ADMIN_ROLE, admin);
        _grantRole(REGISTRAR_ROLE, admin);
    }

    function registerLot(bytes32 lotId, address manufacturer, bytes32 root, uint32 quantity)
        external onlyRole(REGISTRAR_ROLE)
    {
        require(lots[lotId].manufacturer == address(0), "lot exists");
        require(quantity > 0, "qty=0");
        lots[lotId] = Lot(manufacturer, root, quantity, uint64(block.timestamp));
        emit LotRegistered(lotId, manufacturer, root, quantity);
    }

    function anchorBatch(bytes32 root, uint32 count) external onlyRole(REGISTRAR_ROLE) returns (uint256) {
        anchors.push(Anchor(root, count, uint64(block.timestamp)));
        uint256 id = anchors.length - 1;
        emit BatchAnchored(id, root, count);
        return id;
    }

    function freezeLot(bytes32 lotId, bool value) external onlyRole(ADMIN_ROLE) {
        require(lots[lotId].manufacturer != address(0), "unknown lot");
        lotFrozen[lotId] = value;
        emit LotFrozen(lotId, value);
    }

    function setActor(address actor, bool active) external onlyRole(ADMIN_ROLE) {
        activeActor[actor] = active;
        emit ActorSet(actor, active);
    }

    function getAnchor(uint256 id) external view returns (bytes32 root, uint32 count, uint64 ts) {
        Anchor memory a = anchors[id];
        return (a.root, a.count, a.ts);
    }

    function anchorCount() external view returns (uint256) {
        return anchors.length;
    }
}
